"""get_adapter() must select the Improve Digital adapter and forward its full config.

Two regressions pinned here:

1. **Silent mock fallback** — ``get_adapter()`` ends with a mock-adapter
   fallback for unknown types; an adapter registered in ``ADAPTER_REGISTRY``
   but missing from the runtime factory books silently against mock.

2. **Config allowlist drift** — the improvedigital branch copies an explicit
   field allowlist from the validated connection config into the adapter's
   config dict. When the allowlist lags the schema (as happened with the
   campaign-metadata attribution fields: advertiser_uuid, seat_id,
   adops_person_id, ...), tenants that filled those fields still booked
   without campaign metadata — the adapter never saw the values and
   ``_has_campaign_metadata()`` stayed False.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.adapters.improvedigital import ImproveDigitalAdapter
from src.core.helpers.adapter_helpers import get_adapter
from src.core.json_validators import PlatformMappingModel
from src.core.platform_mappings import resolve_adapter_id

pytestmark = pytest.mark.unit

FULL_CONFIG = {
    "client_id": "app-1",
    "client_secret": "s3cret",
    "api_base_url": "https://api.example-360yield.test",
    "improve_demand_contact_id": 7001,
    "buying_entity_id": 9001,
    "buying_entity_office_id": 9002,
    "business_unit_id": 44,
    "buyer_id": 30,
    "agency_id": 182,
    "agency_name": "Example Agency",
    "advertiser_uuid": "00000000-0000-4000-8000-000000000001",
    "advertiser_name": "Example Brand",
    "integration_platform_id": 1,
    "seat_id": "default",
    "adops_person_id": 7002,
    "sales_person_id": "00000000-0000-4000-8000-000000000002",
    "currency": "EUR",
    "timezone": "Europe/Amsterdam",
}


def _build_adapter_via_get_adapter() -> ImproveDigitalAdapter:
    principal = MagicMock()
    principal.principal_id = "p1"
    principal.name = "buyer"
    principal.get_adapter_id.return_value = None

    config_row = MagicMock()
    config_row.adapter_type = "improvedigital"
    config_row.config_json = dict(FULL_CONFIG)

    repo = MagicMock()
    repo.find_by_tenant.return_value = config_row

    with (
        patch("src.core.helpers.adapter_helpers.get_db_session"),
        patch(
            "src.core.database.repositories.adapter_config.AdapterConfigRepository",
            return_value=repo,
        ),
    ):
        adapter = get_adapter(
            principal,
            dry_run=False,
            tenant={"tenant_id": "t1", "ad_server": "improvedigital"},
        )
    return adapter


class TestImproveDigitalRuntimeSelection:
    def test_improvedigital_tenant_gets_the_real_adapter_not_mock(self):
        """The runtime factory has a silent mock fallback for unknown adapter
        types — an improvedigital tenant must NEVER fall through to it."""
        adapter = _build_adapter_via_get_adapter()
        assert isinstance(adapter, ImproveDigitalAdapter)


class TestImproveDigitalConfigForwarding:
    def test_campaign_metadata_fields_reach_the_adapter(self):
        """The attribution fields saved by the settings page must survive
        get_adapter() — otherwise the metadata POST is silently skipped."""
        adapter = _build_adapter_via_get_adapter()

        assert adapter.advertiser_uuid == FULL_CONFIG["advertiser_uuid"]
        assert adapter.advertiser_name == FULL_CONFIG["advertiser_name"]
        assert adapter.agency_id == FULL_CONFIG["agency_id"]
        assert adapter.agency_name == FULL_CONFIG["agency_name"]
        assert adapter.integration_platform_id == FULL_CONFIG["integration_platform_id"]
        assert adapter.seat_id == FULL_CONFIG["seat_id"]
        assert adapter.adops_person_id == FULL_CONFIG["adops_person_id"]
        assert adapter.sales_person_id == FULL_CONFIG["sales_person_id"]
        assert adapter.buyer_id == FULL_CONFIG["buyer_id"]
        assert adapter._has_campaign_metadata() is True

    def test_every_connection_schema_field_is_forwarded(self):
        """Generic guard: any field on ImproveDigitalConnectionConfig that the
        adapter reads from config must come through get_adapter() unchanged,
        so newly added schema fields can never be silently dropped again."""
        adapter = _build_adapter_via_get_adapter()

        for field_name in ImproveDigitalAdapter.connection_config_class.model_fields:
            if field_name not in FULL_CONFIG:
                continue
            assert adapter.config.get(field_name) == FULL_CONFIG[field_name], (
                f"get_adapter() dropped connection-config field {field_name!r} — "
                "add it to the improvedigital branch in adapter_helpers.py"
            )


class TestImproveDigitalPlatformMapping:
    def test_principal_with_only_improvedigital_mapping_validates(self):
        """PlatformMappingModel's at_least_one_platform validator must accept
        a principal whose sole mapping is improvedigital."""
        model = PlatformMappingModel(improvedigital={"advertiser_id": "5001"})
        assert model.improvedigital == {"advertiser_id": "5001"}

    def test_resolve_adapter_id_reads_improvedigital_mapping(self):
        mappings = {"improvedigital": {"advertiser_id": "5001"}}
        assert resolve_adapter_id(mappings, "improvedigital") == "5001"
