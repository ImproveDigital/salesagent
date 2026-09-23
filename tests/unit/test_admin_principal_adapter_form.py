"""Buyer-agent form helpers: adapter detection and platform_mappings assembly.

The Add/Edit Buyer Agent page renders one advertiser control per ad server
(GAM picker, Improve Digital advertiser id, Mock notice). These tests pin
the pure helpers in ``src/admin/blueprints/principals.py`` that drive it.
"""

import pytest
from werkzeug.datastructures import MultiDict

from src.admin.blueprints.principals import _adapter_mappings_from_form, _tenant_adapter_context
from src.core.database.models import AdapterConfig, Tenant


def _tenant(ad_server: str | None, adapter_type: str | None = None) -> Tenant:
    tenant = Tenant(tenant_id="t1", name="Tenant", ad_server=ad_server)
    if adapter_type:
        tenant.adapter_config = AdapterConfig(tenant_id="t1", adapter_type=adapter_type)
    return tenant


class TestTenantAdapterContext:
    def test_gam_tenant(self):
        ctx = _tenant_adapter_context(_tenant("google_ad_manager"))
        assert ctx["has_gam"] is True
        assert ctx["has_improvedigital"] is False
        assert ctx["is_mock"] is False
        assert ctx["adapter_label"] == "Google Ad Manager"

    def test_improvedigital_tenant_is_not_mock(self):
        ctx = _tenant_adapter_context(_tenant("improvedigital"))
        assert ctx["has_gam"] is False
        assert ctx["has_improvedigital"] is True
        assert ctx["is_mock"] is False
        assert ctx["adapter_label"] == "Improve Digital"

    def test_adapter_type_falls_back_to_adapter_config(self):
        ctx = _tenant_adapter_context(_tenant(None, adapter_type="improvedigital"))
        assert ctx["adapter_type"] == "improvedigital"
        assert ctx["has_improvedigital"] is True

    @pytest.mark.parametrize("ad_server", [None, "", "mock"])
    def test_mock_or_unconfigured_tenant(self, ad_server):
        ctx = _tenant_adapter_context(_tenant(ad_server))
        assert ctx["is_mock"] is True
        assert ctx["has_gam"] is False
        assert ctx["adapter_label"] == "Mock"

    def test_adapter_without_principal_mapping(self):
        ctx = _tenant_adapter_context(_tenant("broadstreet"))
        assert ctx["is_mock"] is False
        assert ctx["adapter_label"] == "Broadstreet"


class TestAdapterMappingsFromForm:
    def test_gam_numeric_id(self):
        mappings, error = _adapter_mappings_from_form(MultiDict({"gam_advertiser_id": " 4242 "}), "p1")
        assert error is None
        assert mappings == {"google_ad_manager": {"advertiser_id": "4242", "enabled": True}}

    def test_gam_non_numeric_id_rejected(self):
        mappings, error = _adapter_mappings_from_form(MultiDict({"gam_advertiser_id": "abc"}), "p1")
        assert mappings == {}
        assert error is not None and "GAM Advertiser ID must be numeric" in error

    def test_gam_blank_id_writes_nothing(self):
        mappings, error = _adapter_mappings_from_form(MultiDict({"gam_advertiser_id": ""}), "p1")
        assert (mappings, error) == ({}, None)

    def test_improvedigital_marker_writes_reference_numbers(self):
        form = MultiDict({"improvedigital_mapping": "1"})
        mappings, error = _adapter_mappings_from_form(form, "p1")
        assert error is None
        # No advertiser id per buyer agent: it comes from the tenant connection config.
        assert mappings == {
            "improvedigital": {"enabled": True, "campaign_reference_number": "p1", "line_item_reference_number": "p1"}
        }

    def test_improvedigital_reference_number_is_the_buyer_agent_id_not_form_input(self):
        # Read-only by construction: posted values are ignored, the principal_id wins.
        form = MultiDict(
            {
                "improvedigital_mapping": "1",
                "campaign_reference_number": "tampered",
                "improvedigital_advertiser_id": "9",
            }
        )
        mappings, error = _adapter_mappings_from_form(form, "nike")
        assert error is None
        assert mappings["improvedigital"] == {
            "enabled": True,
            "campaign_reference_number": "nike",
            "line_item_reference_number": "nike",
        }

    def test_improvedigital_marker_absent_writes_nothing(self):
        mappings, error = _adapter_mappings_from_form(MultiDict({"name": "Buyer"}), "p1")
        assert (mappings, error) == ({}, None)

    def test_enable_mock(self):
        mappings, error = _adapter_mappings_from_form(MultiDict({"enable_mock": "1"}), "prin_ab12")
        assert error is None
        assert mappings == {"mock": {"advertiser_id": "mock_prin_ab12", "enabled": True}}
