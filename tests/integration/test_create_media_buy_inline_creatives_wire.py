"""create_media_buy with inline ``packages[].creatives[]`` over the real A2A and MCP wire.

Regression: the canonical bridge translated package-level ``format_option_refs``
but never ran the creative translation over ``packages[].creatives[]``, so the
SDK-normalised canonical creative reached the legacy impl without ``format_id``
(``INVALID_REQUEST[packages.0.creatives.0.format_id]: Field required``) for
both legacy and canonical buyers.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from core.platforms import _canonical_formats as bridge
from tests.integration.test_delegate_wire_envelope_cross_transport import (
    _call_a2a_raw,
    _call_mcp_raw,
    _create_media_buy_payload,
    _extract_a2a_data,
    authenticated_principal,
)

# ``authenticated_principal`` is a pytest fixture re-exported from the cross-transport
# test module; listing it here registers it for this module and keeps F811 quiet.
__all__ = ["authenticated_principal"]

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

REFERENCE_AGENT = "https://creative.adcontextprotocol.org"


def _legacy_creative(creative_id: str) -> dict[str, Any]:
    return {
        "creative_id": creative_id,
        "name": "Inline banner",
        "format_id": {"agent_url": REFERENCE_AGENT, "id": "display_300x250"},
        "assets": {
            "banner_image": {
                "asset_type": "image",
                "url": "https://cdn.example.com/banner.png",
                "width": 300,
                "height": 250,
            }
        },
    }


def _canonical_creative(creative_id: str) -> dict[str, Any]:
    declaration = bridge.declaration_for_legacy_ref({"agent_url": REFERENCE_AGENT, "id": "display_300x250"})
    assert declaration is not None
    return {
        "creative_id": creative_id,
        "name": "Inline banner",
        "format_kind": "image",
        "format_option_ref": {
            "scope": "publisher",
            "publisher_domain": "testbrand.example",
            "format_option_id": declaration.format_option_id,
        },
        "assets": {
            "banner_image": {
                "asset_type": "image",
                "url": "https://cdn.example.com/banner.png",
                "width": 300,
                "height": 250,
            }
        },
    }


def _payload(authenticated_principal: dict[str, str], creative: dict[str, Any], *, adcp_version: str | None) -> dict:
    payload = _create_media_buy_payload(authenticated_principal, idempotency_key=uuid.uuid4().hex, budget=5000.0)
    payload["packages"][0]["creatives"] = [creative]
    if adcp_version:
        payload["adcp_version"] = adcp_version
    return payload


class TestInlinePackageCreativesOverTheWire:
    """Covers: BR-RULE-056-01"""

    def test_a2a_legacy_buyer_inline_creative_is_accepted(self, authenticated_principal):
        body = _call_a2a_raw(
            "create_media_buy",
            _payload(authenticated_principal, _legacy_creative("cr_legacy_a2a"), adcp_version="3.0"),
            authenticated_principal,
        )
        data = _extract_a2a_data(body, expected_state="completed")
        assert data.get("media_buy_id"), data

    def test_a2a_canonical_buyer_inline_creative_is_accepted(self, authenticated_principal):
        body = _call_a2a_raw(
            "create_media_buy",
            _payload(authenticated_principal, _canonical_creative("cr_canon_a2a"), adcp_version="3.1"),
            authenticated_principal,
        )
        data = _extract_a2a_data(body, expected_state="completed")
        assert data.get("media_buy_id"), data

    def test_mcp_legacy_buyer_inline_creative_is_accepted(self, authenticated_principal):
        result = _call_mcp_raw(
            "create_media_buy",
            _payload(authenticated_principal, _legacy_creative("cr_legacy_mcp"), adcp_version="3.0"),
            authenticated_principal,
        )
        assert not result.is_error, result
        assert (result.structured_content or {}).get("media_buy_id"), result.structured_content
