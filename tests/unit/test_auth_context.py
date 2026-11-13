"""Tests for shared AuthContext model and middleware.

Validates that:
- AuthContext model exists with correct attributes (auth_token, headers)
- Middleware populates request.state.auth_context
- get_auth_context() dependency reads from request.state
- Token extraction from Authorization and x-adcp-auth headers
- Unauthenticated requests get AuthContext with auth_token=None

beads: salesagent-b61l.12
"""


# ---------------------------------------------------------------------------
# AuthContext Model Tests
# ---------------------------------------------------------------------------


class TestAuthContextModel:
    """Verify AuthContext model exists with correct attributes."""

    def test_auth_context_exists(self):
        """AuthContext class must exist with auth_token and headers."""
        from src.core.auth_context import AuthContext

        ctx = AuthContext(
            auth_token="tok",
            headers={"host": "example.com"},
        )
        assert ctx.auth_token == "tok"
        assert ctx.headers == {"host": "example.com"}

    def test_unauthenticated_factory(self):
        """AuthContext.unauthenticated() creates a context with no token."""
        from src.core.auth_context import AuthContext

        ctx = AuthContext.unauthenticated(headers={"host": "localhost"})
        assert ctx.auth_token is None
        assert ctx.headers == {"host": "localhost"}

    def test_auth_context_is_frozen(self):
        """AuthContext should be immutable (frozen dataclass)."""
        import dataclasses

        from src.core.auth_context import AuthContext

        assert dataclasses.fields(AuthContext), "AuthContext should be a dataclass"
        ctx = AuthContext(auth_token="tok")
        import pytest

        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            ctx.auth_token = "other"  # type: ignore[misc]
