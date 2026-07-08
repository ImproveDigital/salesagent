"""Tenant-selector recovery and IdP-logout redirect contract.

Regression coverage for bugs found in the auth-fix audit:

- B3: /auth/select-tenant used to bounce an already-authenticated user back
  to /login whenever session["available_tenants"] was missing (e.g. after
  a prior selection consumed it) — forcing a needless re-auth instead of
  just recomputing the list for the already-known user.
- B5: logging out of a tenant with an IdP logout URL configured redirected
  straight to the IdP with no return path, so an IdP that bounces back to
  /login without ?logged_out=1 would have login() auto-redirect the user
  straight back into SSO — "log out" that instantly logs back in.
- C2: choose_tenant.html used to switch between two entirely different page
  layouts — a tenant list OR a "create new account" button — depending on
  whether the user already had tenant access. Now both sections render
  together in multi-tenant mode, regardless of list length.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from tests.unit.conftest import _build_scalars_dispatch


class TestSelectTenantRecovery:
    def test_recomputes_available_tenants_instead_of_redirecting_to_login(self, admin_app):
        canned_tenants = [{"tenant_id": "acme", "name": "Acme", "subdomain": "acme", "is_admin": True}]

        with admin_app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user"] = "user@example.com"
                # Deliberately no "available_tenants" key.

            with patch(
                "src.admin.blueprints.auth._build_available_tenants",
                return_value=canned_tenants,
            ) as mock_build:
                response = client.get("/auth/select-tenant")

            mock_build.assert_called_once_with("user@example.com")
            assert response.status_code == 200
            with client.session_transaction() as sess:
                assert sess["available_tenants"] == canned_tenants

    def test_redirects_to_login_when_no_authenticated_user(self, admin_app):
        with admin_app.test_client() as client:
            response = client.get("/auth/select-tenant")

        assert response.status_code == 302
        assert "/login" in response.location


class TestChooseTenantShowsBothSectionsTogether:
    """Multi-tenant mode must always render the tenant list (or an
    empty-state note) alongside the create-account option — never one
    instead of the other."""

    def test_with_tenants_shows_list_and_create_option(self, admin_app):
        tenants = [{"tenant_id": "acme", "name": "Acme", "subdomain": "acme", "is_admin": True}]
        with admin_app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user"] = "user@example.com"
                sess["available_tenants"] = tenants
            with patch.dict(os.environ, {"ADCP_MULTI_TENANT": "true"}):
                response = client.get("/auth/select-tenant")

        assert response.status_code == 200
        assert b"Select an account to continue" in response.data
        assert b"Create New Account" in response.data

    def test_with_no_tenants_shows_empty_note_and_create_option(self, admin_app):
        with admin_app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user"] = "user@example.com"
                sess["available_tenants"] = []
            with patch.dict(os.environ, {"ADCP_MULTI_TENANT": "true"}):
                response = client.get("/auth/select-tenant")

        assert response.status_code == 200
        assert (
            b"don&#39;t have access to any accounts yet" in response.data
            or b"access to any accounts yet" in response.data
        )
        assert b"Create New Account" in response.data

    def test_single_tenant_mode_with_no_tenants_has_no_create_option(self, admin_app):
        """Single-tenant mode structurally can't have a second tenant, so
        the dead-end state offers logout instead of a create-account link."""
        with admin_app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user"] = "user@example.com"
                sess["available_tenants"] = []
            with patch.dict(os.environ, {"ADCP_MULTI_TENANT": "false"}):
                response = client.get("/auth/select-tenant")

        assert response.status_code == 200
        assert b"Create New Account" not in response.data
        assert b"Log Out" in response.data


class TestLogoutIdpRedirect:
    def test_appends_post_logout_redirect_uri_to_idp_logout_url(self, admin_app):
        from src.core.database.models import Tenant, TenantAuthConfig
        from tests.factories import TenantAuthConfigFactory, TenantFactory

        tenant = TenantFactory.build(tenant_id="acme", name="Acme")
        auth_config = TenantAuthConfigFactory.build(
            tenant_id="acme",
            oidc_logout_url="https://idp.example.com/logout",
        )
        dispatch = _build_scalars_dispatch({Tenant: tenant, TenantAuthConfig: auth_config})

        with admin_app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user"] = "user@example.com"
                sess["tenant_id"] = "acme"

            with patch("src.admin.blueprints.auth.get_db_session") as mock_get_db_session:
                mock_session = mock_get_db_session.return_value.__enter__.return_value
                mock_session.scalars.side_effect = dispatch
                response = client.get("/logout")

        assert response.status_code == 302
        assert response.location.startswith("https://idp.example.com/logout?")
        assert "post_logout_redirect_uri=" in response.location
        assert "logged_out%3D1" in response.location or "logged_out=1" in response.location

    def test_no_idp_url_redirects_to_login_with_logged_out_flag(self, admin_app):
        with admin_app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user"] = "user@example.com"

            response = client.get("/logout")

        assert response.status_code == 302
        assert "/login" in response.location
        assert "logged_out=1" in response.location
