"""get_adapter() must forward the full improvedigital connection config.

Regression: the improvedigital branch of get_adapter() copied an explicit
field allowlist from the validated connection config into the adapter's
config dict. The list predated the campaign-metadata attribution fields
(advertiser_uuid, seat_id, adops_person_id, ...), so tenants that filled
them in the settings page still booked without campaign metadata — the
adapter never saw the values and _has_campaign_metadata() stayed False.
Observed live: campaign 314455 (media buy mb_c2062bc5ca40) booked with a
fully configured tenant but no metadata record on the platform.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.adapters.improvedigital import ImproveDigitalAdapter
from src.core.helpers.adapter_helpers import get_adapter

FULL_CONFIG = {
    "client_id": "app-1",
    "client_secret": "s3cret",
    "api_base_url": "https://api.360yielddev.example",
    "improve_demand_contact_id": 17918,
    "buying_entity_id": 421,
    "buying_entity_office_id": 5068,
    "business_unit_id": 33,
    "buyer_id": 30,
    "agency_id": 182,
    "agency_name": "Other",
    "advertiser_uuid": "b0edd0c5-3fc7-4029-96f0-02e9ff6dca62",
    "advertiser_name": "Other",
    "integration_platform_id": 1,
    "seat_id": "default",
    "adops_person_id": 17922,
    "sales_person_id": "587d62c3-530e-422a-b4f1-d59aafef8067",
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
    assert isinstance(adapter, ImproveDigitalAdapter)
    return adapter


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
