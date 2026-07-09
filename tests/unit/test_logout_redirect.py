"""Tests for post-logout redirect target.

Regression: logout() used to always redirect to auth.login?logged_out=1.
It now prefers the public signup page (public.landing) as a friendlier
landing spot, but must fall back to auth.login?logged_out=1 on a
tenant-shaped host — otherwise public.landing() bounces back to
auth.login() (without logged_out=1) which, if that tenant has SSO
configured, auto-redirects straight back into it, silently undoing the
logout. The decision must stay host-shape-only (no DB lookup) so /logout
never depends on DB availability.
"""

from unittest.mock import patch


class TestLogoutRedirectTarget:
    def test_logout_redirects_to_signup_when_no_sales_agent_domain_configured(self, admin_app):
        """Single-tenant / local dev (no SALES_AGENT_DOMAIN) — always safe."""
        with patch("src.core.domain_config.get_sales_agent_domain", return_value=None):
            with admin_app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["user"] = "user@example.com"

                response = client.get("/logout")

        assert response.status_code == 302
        assert response.location == "/signup"

    def test_logout_redirects_to_signup_on_admin_domain(self):
        with patch("src.core.domain_config.get_sales_agent_domain", return_value="sales-agent.example.com"):
            with patch("src.core.domain_config.get_admin_domain", return_value="admin.sales-agent.example.com"):
                from src.admin.app import create_app

                app = create_app()
                app.config["TESTING"] = True
                app.config["SECRET_KEY"] = "test-secret"

                with app.test_client() as client:
                    with client.session_transaction() as sess:
                        sess["user"] = "user@example.com"

                    response = client.get("/logout", headers={"Host": "admin.sales-agent.example.com"})

        assert response.status_code == 302
        assert response.location == "/signup"

    def test_logout_redirects_to_signup_on_bare_apex(self):
        with patch("src.core.domain_config.get_sales_agent_domain", return_value="sales-agent.example.com"):
            from src.admin.app import create_app

            app = create_app()
            app.config["TESTING"] = True
            app.config["SECRET_KEY"] = "test-secret"

            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["user"] = "user@example.com"

                response = client.get("/logout", headers={"Host": "sales-agent.example.com"})

        assert response.status_code == 302
        assert response.location == "/signup"

    def test_logout_falls_back_to_login_on_tenant_subdomain(self):
        """A tenant subdomain host is conservatively treated as
        tenant-shaped even without confirming a Tenant row exists (no DB
        lookup here) — falls back to the pre-existing safe behavior."""
        with patch("src.core.domain_config.get_sales_agent_domain", return_value="sales-agent.example.com"):
            from src.admin.app import create_app

            app = create_app()
            app.config["TESTING"] = True
            app.config["SECRET_KEY"] = "test-secret"

            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["user"] = "user@example.com"

                response = client.get("/logout", headers={"Host": "acme.sales-agent.example.com"})

        assert response.status_code == 302
        assert response.location == "/login?logged_out=1"

    def test_logout_falls_back_to_login_on_unrelated_custom_domain(self):
        """A host that doesn't match the sales-agent-domain pattern at all
        (a tenant's custom virtual_host) is conservatively treated as risky
        rather than assumed safe."""
        with patch("src.core.domain_config.get_sales_agent_domain", return_value="sales-agent.example.com"):
            from src.admin.app import create_app

            app = create_app()
            app.config["TESTING"] = True
            app.config["SECRET_KEY"] = "test-secret"

            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["user"] = "user@example.com"

                response = client.get("/logout", headers={"Host": "ads.acmepublisher.com"})

        assert response.status_code == 302
        assert response.location == "/login?logged_out=1"

    def test_logout_clears_session_regardless_of_target(self, admin_app):
        """No tenant_id here deliberately — that would exercise the
        separate, pre-existing idp_logout_url DB lookup (unrelated to the
        redirect-target logic this test verifies) and needs its own
        DB-mocked test, not this one."""
        with patch("src.core.domain_config.get_sales_agent_domain", return_value=None):
            with admin_app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["user"] = "user@example.com"
                    sess["some_other_key"] = "some_value"

                client.get("/logout")

                with client.session_transaction() as sess:
                    assert "user" not in sess
                    assert "some_other_key" not in sess
