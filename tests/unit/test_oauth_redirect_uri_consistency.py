"""google_auth() must send the same Google OAuth callback URL regardless
of which URL door the request came through.

Regression: google_auth() is reachable via two doors — bare /auth/google
(e.g. from /signup/start) and /admin/auth/google (e.g. from /admin/login's
auto-redirect). It used to build the callback URL from the current
request (url_for(_external=True)), which reflects whichever door was
used — bare or /admin-prefixed. Google only accepts an exact,
pre-registered URI, so only one door's callback could ever match what
was registered in Google Cloud Console; the other door failed with
Error 400: redirect_uri_mismatch. Observed live: /signup failed while
/admin/auth/select-tenant (which reaches the same view via the /admin
door) succeeded — same bug, same view function, different door.

get_oauth_redirect_uri() fixes this by computing one canonical,
domain-derived callback URL independent of the current request's door.
"""

from unittest.mock import MagicMock, patch


class TestGoogleAuthRedirectUriConsistentAcrossDoors:
    def test_same_redirect_uri_via_bare_and_admin_doors(self, admin_app):
        """Simulates the exact live bug: hitting google_auth() through the
        bare door vs. through the /admin-prefixed door (SCRIPT_NAME=/admin,
        matching what the ASGI admin_mount middleware sets) must no longer
        produce two different callback URLs."""
        mock_response = MagicMock()
        mock_response.headers = {}

        with patch("src.core.domain_config.get_sales_agent_domain", return_value="sales-agent.example.com"):
            with admin_app.test_client() as client:
                with patch.object(admin_app.oauth.google, "authorize_redirect", return_value=mock_response) as m:
                    client.get("/auth/google")
                bare_door_uri = m.call_args.args[0]

            with admin_app.test_client() as client:
                with patch.object(admin_app.oauth.google, "authorize_redirect", return_value=mock_response) as m:
                    client.get("/auth/google", environ_overrides={"SCRIPT_NAME": "/admin"})
                admin_door_uri = m.call_args.args[0]

        assert bare_door_uri == admin_door_uri
        assert bare_door_uri == "https://sales-agent.example.com/admin/auth/google/callback"

    def test_tenant_google_auth_matches_same_canonical_uri(self, admin_app):
        """tenant_google_auth() (the /admin/auth/select-tenant door in the
        live report) must compute the identical canonical URI as
        google_auth() — both feed the same Google OAuth client."""
        mock_response = MagicMock()
        mock_response.headers = {}

        with patch("src.core.domain_config.get_sales_agent_domain", return_value="sales-agent.example.com"):
            with admin_app.test_client() as client:
                with patch.object(admin_app.oauth.google, "authorize_redirect", return_value=mock_response) as m:
                    client.get("/tenant/some_tenant/auth/google")
                tenant_door_uri = m.call_args.args[0]

        assert tenant_door_uri == "https://sales-agent.example.com/admin/auth/google/callback"

    def test_falls_back_to_request_relative_uri_without_sales_agent_domain(self, admin_app):
        """Pure local dev (no SALES_AGENT_DOMAIN) — no multi-door ambiguity,
        so the request-relative URL is fine."""
        mock_response = MagicMock()
        mock_response.headers = {}

        with patch("src.core.domain_config.get_sales_agent_domain", return_value=None):
            with admin_app.test_client() as client:
                with patch.object(admin_app.oauth.google, "authorize_redirect", return_value=mock_response) as m:
                    client.get("/auth/google")

        assert m.call_args.args[0] == "http://localhost/auth/google/callback"
