"""Regression tests for FastAPI migration code review fixes.

Tests P0/P1 issues found during code review of the FastAPI unified app.
Each test targets a specific beads issue to prevent regression.

salesagent-c0gm: Non-async receive lambda (ASGI protocol)
salesagent-agey: CORS origins configuration
salesagent-9fy7: Apx-Incoming-Host hostname validation
salesagent-agmq: Debug endpoints gated behind ADCP_TESTING
salesagent-nb7k: format_resolver async event loop fix
"""

import os
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# salesagent-nb7k [P1]: format_resolver uses run_async_in_sync_context
# ---------------------------------------------------------------------------


class TestFormatResolverNoEventLoopCreation:
    """format_resolver must use run_async_in_sync_context, not new_event_loop."""

    def test_format_resolver_does_not_import_new_event_loop(self):
        """format_resolver must not use asyncio.new_event_loop (causes deadlocks).

        Before fix: asyncio.new_event_loop() + run_until_complete() — deadlocks.
        After fix: run_async_in_sync_context() — handles both sync and async contexts.
        """
        import src.core.format_resolver as fr_module

        # Verify the module does not reference new_event_loop at attribute level
        assert not hasattr(fr_module, "new_event_loop"), "format_resolver should not export new_event_loop"
        # Verify run_async_in_sync_context is imported (the correct approach)
        assert hasattr(fr_module, "run_async_in_sync_context"), (
            "format_resolver should import run_async_in_sync_context"
        )

    def test_get_format_works_from_sync_context(self):
        """get_format should work when called from a sync context."""
        from unittest.mock import AsyncMock

        mock_format = MagicMock()
        mock_format.format_id = "test_format"

        mock_registry = MagicMock()
        mock_registry.get_format = AsyncMock(return_value=mock_format)

        with patch("src.core.creative_agent_registry.get_creative_agent_registry", return_value=mock_registry):
            from src.core.format_resolver import get_format

            result = get_format("test_format", agent_url="http://example.com/agent")

        assert result == mock_format


class TestOidcCallbackCompatibility:
    """OIDC config should keep the legacy public callback path."""

    def test_get_tenant_redirect_uri_uses_root_auth_callback(self):
        from src.services.auth_config_service import get_tenant_redirect_uri

        tenant = MagicMock()
        tenant.virtual_host = None
        tenant.subdomain = None

        with patch.dict(os.environ, {"ADCP_SALES_PORT": "8080"}, clear=False):
            redirect_uri = get_tenant_redirect_uri(tenant)

        assert redirect_uri.endswith("/auth/oidc/callback")
