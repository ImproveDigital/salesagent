"""``_validate_token`` tolerates a ``Bearer`` scheme on the legacy ``x-adcp-auth`` alias.

The adcp Python client sends ``x-adcp-auth: Bearer <token>`` when configured
with ``auth_type="bearer"`` and the default header; the SDK middleware hands
legacy-alias values to the validator verbatim.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from core import main as core_main


def test_bearer_scheme_on_alias_is_stripped_before_lookup():
    seen: list[str] = []

    def fake_resolve(token: str):
        seen.append(token)
        return SimpleNamespace(principal_id="p1", tenant_id="t1")

    with patch.object(core_main, "resolve_embedded_identity_token", side_effect=fake_resolve):
        principal = core_main._validate_token("Bearer  emb_abc123 ")

    assert seen == ["emb_abc123"]
    assert principal is not None
    assert (principal.caller_identity, principal.tenant_id) == ("p1", "t1")


def test_raw_token_is_passed_through_unchanged():
    seen: list[str] = []

    def fake_resolve(token: str):
        seen.append(token)
        return SimpleNamespace(principal_id="p1", tenant_id="t1")

    with patch.object(core_main, "resolve_embedded_identity_token", side_effect=fake_resolve):
        core_main._validate_token("raw_token_value")

    assert seen == ["raw_token_value"]


def test_empty_token_is_rejected():
    assert core_main._validate_token("") is None
