"""GAMAuthManager mints REST-scoped bearer headers."""

import json
from unittest.mock import Mock, patch

from src.adapters.gam.auth import GAMAuthManager

_SA = json.dumps(
    {
        "type": "service_account",
        "client_email": "x@y.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
)


def test_service_account_header_uses_rest_readonly_scope():
    creds = Mock()
    creds.token = "sa-token"
    with (
        patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=creds) as factory,
        patch("google.auth.transport.requests.Request"),
    ):
        header = GAMAuthManager({"service_account_json": _SA}).get_rest_auth_header()

    assert header == {"Authorization": "Bearer sa-token"}
    assert factory.call_args.kwargs["scopes"] == ["https://www.googleapis.com/auth/admanager.readonly"]
    creds.refresh.assert_called_once()


def test_oauth_header_reuses_googleads_refresh_client():
    manager = GAMAuthManager({"refresh_token": "rt"})
    oauth_client = Mock()
    oauth_client.CreateHttpHeader.return_value = {"Authorization": "Bearer oauth-token"}
    with patch.object(manager, "_get_oauth_credentials", return_value=oauth_client):
        assert manager.get_rest_auth_header() == {"Authorization": "Bearer oauth-token"}
