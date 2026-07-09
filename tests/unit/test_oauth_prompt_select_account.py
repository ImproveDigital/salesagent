"""Google OAuth initiation always requests prompt=select_account.

Regression: without this, logging out of the app doesn't feel like
logging out — /login (or /tenant/<id>/login) auto-redirects back into
Google OAuth, and if the browser still has an active Google session,
Google silently re-authenticates with no visible prompt, landing the
user right back in. prompt=select_account forces Google's account
chooser on every login attempt instead of transparently reusing the
existing IdP session.
"""

from unittest.mock import MagicMock, patch


class TestGoogleAuthRequestsAccountChooser:
    def test_google_auth_passes_prompt_select_account(self, admin_app):
        mock_response = MagicMock()
        mock_response.headers = {}

        with admin_app.test_client() as client:
            with patch.object(admin_app.oauth.google, "authorize_redirect", return_value=mock_response) as m:
                client.get("/auth/google")

        m.assert_called_once()
        assert m.call_args.kwargs.get("prompt") == "select_account"

    def test_tenant_google_auth_passes_prompt_select_account(self, admin_app):
        mock_response = MagicMock()
        mock_response.headers = {}

        with admin_app.test_client() as client:
            with patch.object(admin_app.oauth.google, "authorize_redirect", return_value=mock_response) as m:
                client.get("/tenant/some_tenant/auth/google")

        m.assert_called_once()
        assert m.call_args.kwargs.get("prompt") == "select_account"
