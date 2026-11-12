"""Regression tests for REST Depends-based auth resolution.

Validates that REST routes use FastAPI Depends() for identity resolution
instead of manual _resolve_auth/_require_auth calls inside handler bodies.

Core invariant: Identity resolution for REST routes is declared in function
signatures via FastAPI Depends, never called manually inside handler bodies.

beads: salesagent-lkyi
"""

from fastapi.params import Depends


class TestResolveAuthDependencyExists:
    """auth_context.py should export resolve_auth and require_auth Depends."""

    def test_resolve_auth_exists_in_auth_context(self):
        """resolve_auth should be exported from auth_context."""
        from src.core.auth_context import resolve_auth

        assert resolve_auth is not None

    def test_require_auth_exists_in_auth_context(self):
        """require_auth should be exported from auth_context."""
        from src.core.auth_context import require_auth

        assert require_auth is not None

    def test_resolve_auth_is_depends_instance(self):
        """resolve_auth should be a FastAPI Depends instance."""
        from src.core.auth_context import resolve_auth

        assert isinstance(resolve_auth, Depends), f"resolve_auth is {type(resolve_auth).__name__}, expected Depends"

    def test_require_auth_is_depends_instance(self):
        """require_auth should be a FastAPI Depends instance."""
        from src.core.auth_context import require_auth

        assert isinstance(require_auth, Depends), f"require_auth is {type(require_auth).__name__}, expected Depends"


class TestResolveAuthDepBehavior:
    """Test the resolve_auth dependency function behavior directly."""

    def test_returns_none_without_token(self):
        """resolve_auth dep should return None when no auth token present."""
        from src.core.auth_context import AuthContext, _resolve_auth_dep

        auth_ctx = AuthContext.unauthenticated()
        result = _resolve_auth_dep(auth_ctx)
        assert result is None

    def test_returns_identity_with_valid_token(self):
        """resolve_auth dep should return ResolvedIdentity with valid token."""
        from unittest.mock import patch

        from src.core.auth_context import AuthContext, _resolve_auth_dep
        from src.core.resolved_identity import ResolvedIdentity

        auth_ctx = AuthContext(
            auth_token="test-token",
            headers={"x-adcp-auth": "test-token"},
        )

        mock_identity = ResolvedIdentity(
            principal_id="test_principal",
            tenant_id="default",
            tenant={"tenant_id": "default"},
            protocol="rest",
        )

        with patch("src.core.resolved_identity.resolve_identity", return_value=mock_identity):
            result = _resolve_auth_dep(auth_ctx)

        assert isinstance(result, ResolvedIdentity)
        assert result.principal_id == "test_principal"

    def test_passes_auth_token_to_resolve_identity(self):
        """resolve_auth dep should pass pre-extracted token to avoid redundant extraction."""
        from unittest.mock import patch

        from src.core.auth_context import AuthContext, _resolve_auth_dep
        from src.core.resolved_identity import ResolvedIdentity

        auth_ctx = AuthContext(
            auth_token="pre-extracted-token",
            headers={"authorization": "Bearer pre-extracted-token"},
        )

        mock_identity = ResolvedIdentity(
            principal_id="test_principal",
            tenant_id="default",
            tenant={"tenant_id": "default"},
            protocol="rest",
        )

        with patch("src.core.resolved_identity.resolve_identity", return_value=mock_identity) as mock_resolve:
            _resolve_auth_dep(auth_ctx)

        mock_resolve.assert_called_once()
        call_kwargs = mock_resolve.call_args
        assert call_kwargs.kwargs.get("auth_token") == "pre-extracted-token" or (
            len(call_kwargs.args) > 1 and call_kwargs.args[1] == "pre-extracted-token"
        )


class TestRequireAuthDepBehavior:
    """Test the require_auth dependency function behavior directly."""

    def test_raises_without_token(self):
        """require_auth dep should raise AdCPAuthenticationError without token."""
        import pytest

        from src.core.auth_context import AuthContext, _require_auth_dep
        from src.core.exceptions import AdCPAuthenticationError

        auth_ctx = AuthContext.unauthenticated()
        with pytest.raises(AdCPAuthenticationError, match="Authentication required"):
            _require_auth_dep(auth_ctx)

    def test_returns_identity_with_valid_token(self):
        """require_auth dep should return ResolvedIdentity with valid token."""
        from unittest.mock import patch

        from src.core.auth_context import AuthContext, _require_auth_dep
        from src.core.resolved_identity import ResolvedIdentity

        auth_ctx = AuthContext(
            auth_token="test-token",
            headers={"x-adcp-auth": "test-token"},
        )

        mock_identity = ResolvedIdentity(
            principal_id="test_principal",
            tenant_id="default",
            tenant={"tenant_id": "default"},
            protocol="rest",
        )

        with patch("src.core.resolved_identity.resolve_identity", return_value=mock_identity):
            result = _require_auth_dep(auth_ctx)

        assert isinstance(result, ResolvedIdentity)
        assert result.principal_id == "test_principal"
