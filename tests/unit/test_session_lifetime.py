"""Session lifetime configuration contract.

Without an explicit ``PERMANENT_SESSION_LIFETIME``, Flask sessions are
browser-session cookies with no idle timeout and no absolute lifetime — an
undocumented default, not a deliberate choice. This locks in an explicit,
configurable lifetime so admin sessions expire predictably.
"""

from __future__ import annotations

import os
from datetime import timedelta
from unittest.mock import patch

import pytest

from src.admin.app import create_app


class TestSessionLifetimeConfig:
    def test_permanent_session_lifetime_configured(self):
        app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        assert app.config["PERMANENT_SESSION_LIFETIME"].total_seconds() > 0

    def test_permanent_session_lifetime_defaults_to_12_hours(self):
        env = {k: v for k, v in os.environ.items() if k != "SESSION_LIFETIME_HOURS"}
        with patch.dict(os.environ, env, clear=True):
            app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        assert app.config["PERMANENT_SESSION_LIFETIME"] == timedelta(hours=12)

    def test_session_lifetime_configurable_via_env(self):
        with patch.dict(os.environ, {"SESSION_LIFETIME_HOURS": "2"}):
            app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        assert app.config["PERMANENT_SESSION_LIFETIME"] == timedelta(hours=2)

    def test_session_refresh_each_request_enabled(self):
        app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        assert app.config["SESSION_REFRESH_EACH_REQUEST"] is True


class TestFlaskSecretKeyRequirement:
    """FLASK_SECRET_KEY must be explicit in production; a missing key falls
    back to a random per-process value that invalidates every session on
    restart (and diverges across workers)."""

    def test_missing_secret_key_raises_in_production(self):
        env = {k: v for k, v in os.environ.items() if k != "FLASK_SECRET_KEY"}
        env["PRODUCTION"] = "true"
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="FLASK_SECRET_KEY"):
                create_app({"TESTING": True})

    def test_missing_secret_key_falls_back_outside_production(self):
        env = {k: v for k, v in os.environ.items() if k not in ("FLASK_SECRET_KEY", "PRODUCTION", "ENVIRONMENT")}
        with patch.dict(os.environ, env, clear=True):
            app = create_app({"TESTING": True})
        assert app.secret_key

    def test_explicit_secret_key_used_in_production(self):
        env = {k: v for k, v in os.environ.items() if k != "FLASK_SECRET_KEY"}
        env["PRODUCTION"] = "true"
        env["FLASK_SECRET_KEY"] = "a-stable-production-key"
        with patch.dict(os.environ, env, clear=True):
            app = create_app({"TESTING": True})
        assert app.secret_key == "a-stable-production-key"


class TestSessionPermanenceAtLogin:
    """Every login path must mark the session permanent so
    PERMANENT_SESSION_LIFETIME actually applies — otherwise the cookie is a
    browser-session cookie regardless of the configured lifetime."""

    def test_test_auth_marks_session_permanent(self, make_auth_test_client):
        with make_auth_test_client(auth_setup_mode=True) as (client, _):
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true", "PRODUCTION": "", "ENVIRONMENT": ""}):
                response = client.post(
                    "/test/auth",
                    data={"email": "test_super_admin@example.com", "password": "test123", "tenant_id": "default"},
                )
            assert response.status_code == 302
            with client.session_transaction() as sess:
                assert sess.permanent is True

    def test_google_callback_marks_session_permanent(self):
        """Drives the real /auth/google/callback route with OAuth token
        exchange mocked, taking the super-admin branch so no DB is needed."""
        with patch.dict(
            os.environ,
            {
                "GOOGLE_CLIENT_ID": "test-client-id",
                "GOOGLE_CLIENT_SECRET": "test-client-secret",
                "SUPER_ADMIN_DOMAINS": "example.com",
            },
        ):
            app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})

            with app.test_client() as client:
                with patch.object(app.oauth.google, "authorize_access_token", return_value={"access_token": "tok"}):
                    with patch(
                        "src.admin.blueprints.auth.extract_user_info",
                        return_value={"email": "admin@example.com", "name": "Admin"},
                    ):
                        response = client.get("/auth/google/callback")

                assert response.status_code == 302
                with client.session_transaction() as sess:
                    assert sess.permanent is True
                    assert sess.get("user") == "admin@example.com"
                    # base.html's identity/logout block gates on these two
                    # keys — regression coverage for a real Google OAuth
                    # login rendering no logout button at all.
                    assert sess.get("authenticated") is True
                    assert sess.get("email") == "admin@example.com"
