"""CreativeFormatsEnv — integration test environment for _list_creative_formats_impl.

Patches: creative agent registry, audit logger.
Real: format processing logic (no direct DB access in this _impl).

Requires: integration_db fixture (creates test PostgreSQL DB).

Usage::

    @pytest.mark.requires_db
    def test_something(self, integration_db):
        with CreativeFormatsEnv() as env:
            env.set_registry_formats([mock_format_1, mock_format_2])
            response = env.call_impl()
            assert len(response.formats) == 2

Available mocks via env.mock:
    "registry"     -- get_creative_agent_registry (lazy import in creative_formats.py)
    "audit_logger" -- get_audit_logger (module-level import in creative_formats.py)

Transport support:
    call_impl(**kw)     -- direct _list_creative_formats_impl
    call_mcp(**kw)      -- list_creative_formats via Client(mcp) -> ListCreativeFormatsResponse
    call_mcp_raw(**kw)  -- same MCP path, returns the raw fastmcp CallToolResult
    call_a2a(**kw)      -- list_creative_formats via A2A JSON-RPC -> ListCreativeFormatsResponse
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from src.core.schemas import ListCreativeFormatsResponse
from tests.harness._base import IntegrationEnv


class CreativeFormatsEnv(IntegrationEnv):
    """Integration test environment for _list_creative_formats_impl.

    Mocks creative agent registry (external service) and audit logger.
    The format processing logic runs for real.
    """

    EXTERNAL_PATCHES = {
        "registry": "src.core.creative_agent_registry.get_creative_agent_registry",
        "audit_logger": "src.core.tools.creative_formats.get_audit_logger",
    }

    def _configure_mocks(self) -> None:
        """Set up happy-path defaults for external mocks.

        Seeds a minimal set of default formats so scenarios that don't
        explicitly call set_registry_formats() still get non-empty results.
        Scenarios needing specific formats override via set_registry_formats().
        """
        from src.core.creative_agent_registry import FormatFetchResult, _get_mock_formats

        default_formats = _get_mock_formats()

        # Registry: return a mock with async list_all_formats + list_all_formats_with_errors
        mock_registry = MagicMock()
        mock_registry.list_all_formats = AsyncMock(return_value=default_formats)
        mock_registry.list_all_formats_with_errors = AsyncMock(
            return_value=FormatFetchResult(formats=default_formats, errors=[])
        )
        self.mock["registry"].return_value = mock_registry

        # Audit logger: no-op
        mock_logger = MagicMock()
        self.mock["audit_logger"].return_value = mock_logger

    def set_registry_formats(self, formats: list[Any]) -> None:
        """Configure mock registry to return these formats from list_all_formats."""
        from src.core.creative_agent_registry import FormatFetchResult

        self.mock["registry"].return_value.list_all_formats = AsyncMock(return_value=formats)
        self.mock["registry"].return_value.list_all_formats_with_errors = AsyncMock(
            return_value=FormatFetchResult(formats=list(formats), errors=[])
        )

    def call_impl(self, **kwargs: Any) -> ListCreativeFormatsResponse:
        """Call _list_creative_formats_impl.

        Accepts 'req' (ListCreativeFormatsRequest) and 'identity' kwargs.
        Defaults to self.identity if not provided.
        """
        from src.core.tools.creative_formats import _list_creative_formats_impl

        self._commit_factory_data()
        kwargs.setdefault("identity", self.identity)
        kwargs.setdefault("req", None)
        return _list_creative_formats_impl(**kwargs)

    def call_mcp(self, **kwargs: Any) -> ListCreativeFormatsResponse:
        """Call list_creative_formats via Client(mcp) — full pipeline dispatch."""
        return self._run_mcp_client("list_creative_formats", ListCreativeFormatsResponse, **kwargs)

    def call_mcp_raw(self, **kwargs: Any) -> Any:
        """Call list_creative_formats via Client(mcp) and return the raw ``CallToolResult``.

        Same in-process pipeline as :meth:`call_mcp` (bearer middleware, adcp
        SDK dispatcher, ``list_creative_formats_legacy`` handler), but skips
        the ``ListCreativeFormatsResponse`` parsing so tests can assert on the
        MCP tool-result envelope itself: ``content`` (TextContent blocks) and
        ``structured_content`` (the JSON payload).
        """
        import httpx
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        from tests.harness._asgi_app import run_on_app_loop
        from tests.harness.transport import Transport

        self._commit_factory_data()

        _NO_OVERRIDE = object()
        identity = kwargs.pop("identity", _NO_OVERRIDE)
        mcp_identity = self.identity_for(Transport.MCP) if identity is _NO_OVERRIDE else identity

        req = kwargs.pop("req", None)
        if req is not None and hasattr(req, "model_dump"):
            arguments = {**req.model_dump(exclude_none=True), **kwargs}
        else:
            arguments = dict(kwargs)

        auth_token = mcp_identity.auth_token if mcp_identity else None
        if not auth_token and self._session is not None:
            auth_token = self._ensure_principal_for_mcp(mcp_identity)

        request_headers = {"x-adcp-auth": auth_token or "test-stub-token"}
        if mcp_identity and mcp_identity.tenant_id:
            request_headers["x-adcp-tenant"] = mcp_identity.tenant_id

        def _factory(app: Any) -> Any:
            def httpx_factory(**hk: Any) -> httpx.AsyncClient:
                hk.setdefault("timeout", 30.0)
                hk["transport"] = httpx.ASGITransport(app=app)
                hk["base_url"] = "http://testserver"
                return httpx.AsyncClient(**hk)

            transport = StreamableHttpTransport(
                url="http://testserver/mcp/",
                headers=request_headers,
                httpx_client_factory=httpx_factory,
            )

            async def _call() -> Any:
                async with Client(transport) as client:
                    return await client.call_tool("list_creative_formats", arguments)

            return _call()

        return run_on_app_loop(_factory)

    def call_a2a(self, **kwargs: Any) -> ListCreativeFormatsResponse:
        """Call list_creative_formats via A2A JSON-RPC — full pipeline dispatch.

        The A2A surface has no in-process identity injection: tenant context
        is resolved from the ``x-adcp-tenant`` header + bearer token by the
        production auth chain. When that chain cannot resolve a tenant it
        rejects the request with HTTP 401, which is surfaced here as
        :class:`AdCPAuthenticationError` (mirroring the 401 handling in
        ``_unwrap_mcp_tool_error`` on the MCP path).
        """
        import httpx

        from src.core.exceptions import AdCPAuthenticationError

        try:
            return self._run_a2a_client("list_creative_formats", ListCreativeFormatsResponse, **kwargs)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401:
                raise
            identity = kwargs.get("identity")
            tenant_id = getattr(identity, "tenant_id", None) or self._tenant_id
            try:
                description = exc.response.json().get("error_description")
            except ValueError:
                description = None
            message = (
                f"Authentication failed: no tenant context could be resolved for tenant '{tenant_id}' "
                f"(A2A rejected the request with HTTP 401: {description or exc.response.text})"
            )
            raise AdCPAuthenticationError(
                message,
                details={"suggestion": message, "tenant_id": tenant_id},
                recovery="correctable",
            ) from exc
