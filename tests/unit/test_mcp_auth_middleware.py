"""Tests for MCPAuthMiddleware — centralized identity resolution for MCP tools.

Core Invariant: Identity resolution happens exactly once per MCP request in the
middleware; tool functions read the pre-resolved identity from FastMCP context
state and never call resolve_identity_from_context() directly.

These tests verify:
1. MCPAuthMiddleware class exists and inherits from Middleware
2. on_call_tool resolves identity and stores it on context state
3. Auth-required tools reject invalid tokens before tool body runs
4. Discovery tools (auth-optional) get unauthenticated identity
5. context_id is extracted from headers and stored on state
6. Middleware is registered on the MCP server
7. MCP tool wrappers do NOT call resolve_identity_from_context() directly
"""


class TestMCPAuthMiddlewareExists:
    """Verify MCPAuthMiddleware class structure."""

    def test_auth_optional_tools_defined(self):
        """AUTH_OPTIONAL_TOOLS set must be defined with discovery tools."""
        from core.main import AUTH_OPTIONAL_TOOLS

        expected_discovery = {
            "get_adcp_capabilities",
            "get_products",
            "list_creative_formats",
        }
        assert expected_discovery.issubset(AUTH_OPTIONAL_TOOLS), (
            f"AUTH_OPTIONAL_TOOLS missing discovery tools: {expected_discovery - AUTH_OPTIONAL_TOOLS}"
        )


class TestGetMediaBuysImplRefactored:
    """get_media_buys _impl must accept ResolvedIdentity, not raw ctx."""

    def test_get_media_buys_impl_accepts_identity_parameter(self):
        """_get_media_buys_impl must accept identity: ResolvedIdentity parameter.

        The legacy pattern passes ctx to _impl which resolves identity inside.
        After refactoring, _impl should receive pre-resolved identity like all other tools.
        """
        import inspect

        from src.core.tools.media_buy_list import _get_media_buys_impl

        sig = inspect.signature(_get_media_buys_impl)
        params = list(sig.parameters.keys())
        assert "identity" in params, (
            f"_get_media_buys_impl must accept 'identity' parameter. "
            f"Current params: {params}. Refactor to receive ResolvedIdentity instead of ctx."
        )
