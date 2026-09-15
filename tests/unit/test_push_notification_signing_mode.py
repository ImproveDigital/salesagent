"""Signing-mode inference for buyer push_notification_config registrations.

AdCP 3.x makes RFC 9421 the baseline. Only the deprecated legacy schemes
(``HMAC-SHA256`` shared secret, ``Bearer`` token) may select the legacy
``hmac`` signing mode. Any other scheme — including the spec's
``HTTP_MESSAGE_SIGNATURES`` marker — must resolve to ``rfc9421`` so the
webhook is never sent unsigned.
"""

import pytest

from src.services.push_notification_registration import normalize_push_notification_config

_URL = "https://buyer.example.com/webhooks/adcp"


def _normalize(authentication: dict | None):
    config: dict = {"url": _URL, "operation_id": "op-1"}
    if authentication is not None:
        config["authentication"] = authentication
    registration = normalize_push_notification_config(config)
    assert registration is not None
    return registration


def test_http_message_signatures_scheme_selects_rfc9421():
    registration = _normalize({"schemes": ["HTTP_MESSAGE_SIGNATURES"]})
    assert registration.signing_mode == "rfc9421"
    assert registration.authentication_type == "HTTP_MESSAGE_SIGNATURES"


def test_unknown_scheme_defaults_to_rfc9421():
    registration = _normalize({"schemes": ["SomethingNew"]})
    assert registration.signing_mode == "rfc9421"


def test_no_authentication_block_selects_rfc9421():
    registration = _normalize(None)
    assert registration.signing_mode == "rfc9421"
    assert registration.authentication_type is None


@pytest.mark.parametrize(
    "scheme",
    ["HMAC-SHA256", "hmac-sha256", "Bearer", "bearer"],
)
def test_legacy_schemes_select_hmac(scheme: str):
    registration = _normalize({"schemes": [scheme], "credentials": "x" * 40})
    assert registration.signing_mode == "hmac"
    assert registration.authentication_type == scheme
