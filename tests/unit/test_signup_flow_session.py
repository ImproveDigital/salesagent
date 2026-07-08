"""Tests for signup flow session handling.

Ensures that signup session state (signup_flow, signup_step) is preserved
through the OAuth flow, fixing the bug where session.clear() wiped signup state.
"""

from unittest.mock import MagicMock, patch


class TestSignupFlowSessionPreservation:
    """Test that signup flow state survives OAuth redirect."""

    def test_google_auth_preserves_signup_flow_state(self):
        """Verify session.clear() preserves signup_flow and signup_step.

        Regression test for: New users unable to create accounts because
        session.clear() in google_auth() wiped signup_flow state.
        """
        with patch.dict(
            "os.environ",
            {
                "GOOGLE_CLIENT_ID": "test-client-id",
                "GOOGLE_CLIENT_SECRET": "test-client-secret",
            },
        ):
            from src.admin.app import create_app

            app = create_app()
            app.config["TESTING"] = True
            app.config["SECRET_KEY"] = "test-secret"

            with app.test_client() as client:
                # Simulate signup flow start - sets session flags
                with client.session_transaction() as sess:
                    sess["signup_flow"] = True
                    sess["signup_step"] = "oauth"

                # Mock OAuth to avoid actual redirect
                with patch.object(app.oauth.google, "authorize_redirect") as mock_redirect:
                    mock_response = MagicMock()
                    mock_response.headers = {}
                    mock_redirect.return_value = mock_response

                    # Call google_auth (this used to clear session completely)
                    client.get("/auth/google")

                # Verify signup state was preserved
                with client.session_transaction() as sess:
                    assert sess.get("signup_flow") is True, "signup_flow should be preserved through OAuth redirect"
                    assert sess.get("signup_step") == "oauth", "signup_step should be preserved through OAuth redirect"

    def test_google_auth_clears_other_session_data(self):
        """Verify session.clear() still clears non-signup session data."""
        with patch.dict(
            "os.environ",
            {
                "GOOGLE_CLIENT_ID": "test-client-id",
                "GOOGLE_CLIENT_SECRET": "test-client-secret",
            },
        ):
            from src.admin.app import create_app

            app = create_app()
            app.config["TESTING"] = True
            app.config["SECRET_KEY"] = "test-secret"

            with app.test_client() as client:
                # Set various session data including signup flow
                with client.session_transaction() as sess:
                    sess["signup_flow"] = True
                    sess["signup_step"] = "oauth"
                    sess["old_user"] = "should-be-cleared@example.com"
                    sess["stale_tenant_id"] = "old-tenant-123"

                # Mock OAuth to avoid actual redirect
                with patch.object(app.oauth.google, "authorize_redirect") as mock_redirect:
                    mock_response = MagicMock()
                    mock_response.headers = {}
                    mock_redirect.return_value = mock_response

                    client.get("/auth/google")

                # Verify signup state preserved but other data cleared
                with client.session_transaction() as sess:
                    assert sess.get("signup_flow") is True
                    assert sess.get("signup_step") == "oauth"
                    assert "old_user" not in sess, "Old user data should be cleared"
                    assert "stale_tenant_id" not in sess, "Stale tenant data should be cleared"

    def test_google_auth_without_signup_flow(self):
        """Verify normal login (no signup) works without signup state."""
        with patch.dict(
            "os.environ",
            {
                "GOOGLE_CLIENT_ID": "test-client-id",
                "GOOGLE_CLIENT_SECRET": "test-client-secret",
            },
        ):
            from src.admin.app import create_app

            app = create_app()
            app.config["TESTING"] = True
            app.config["SECRET_KEY"] = "test-secret"

            with app.test_client() as client:
                # Normal login - no signup flow
                with client.session_transaction() as sess:
                    sess["some_data"] = "test"

                # Mock OAuth
                with patch.object(app.oauth.google, "authorize_redirect") as mock_redirect:
                    mock_response = MagicMock()
                    mock_response.headers = {}
                    mock_redirect.return_value = mock_response

                    client.get("/auth/google")

                # Session should be mostly clear, no signup state
                with client.session_transaction() as sess:
                    assert "signup_flow" not in sess
                    assert "signup_step" not in sess
                    assert "some_data" not in sess


class TestOnboardingReachableFromEitherDoor:
    """Regression: /signup/onboarding used to require session["signup_flow"],
    which is only set by /signup/start. A user arriving via the tenant
    selector's "Create New Account" link (login path, not signup path) hit
    "Invalid signup session" even though they were fully authenticated —
    the same button worked from one entry door and not the other.

    Authentication is the only requirement that actually matters here:
    signup_flow was never a security boundary (anyone authenticated could
    already load this URL directly), just an incidental gate that happened
    to diverge between the two doors.
    """

    def test_onboarding_renders_for_authenticated_user_without_signup_flow(self, admin_app):
        with admin_app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user"] = "user@example.com"
                sess["user_name"] = "Example User"
                # Deliberately no "signup_flow" — simulates arriving via the
                # tenant selector's "Create New Account" link, not /signup/start.

            response = client.get("/signup/onboarding")

        assert response.status_code == 200
        assert b"Invalid signup session" not in response.data

    def test_onboarding_still_requires_authentication(self, admin_app):
        with admin_app.test_client() as client:
            response = client.get("/signup/onboarding")

        assert response.status_code == 302
        assert "/signup" in response.location
