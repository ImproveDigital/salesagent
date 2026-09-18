"""Tests for the Improve Digital adapter — registry wiring + Classic dry-run behaviour."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from src.adapters import get_adapter_default_channels, get_adapter_schemas
from src.adapters.improvedigital import ImproveDigitalAdapter
from src.adapters.improvedigital.schemas import ImproveDigitalConnectionConfig, ImproveDigitalProductConfig
from tests.helpers.adapter_test_helpers import (
    invoke_create_media_buy,
    make_sample_create_request,
    make_sample_video_package,
)


@pytest.fixture
def mock_principal():
    principal = MagicMock()
    principal.name = "classic_advertiser"
    principal.principal_id = "principal_impd_1"
    # Advertiser IDs are integers in the live API; a numeric string keeps
    # casts clean on the CampaignDto advertiserId field.
    principal.get_adapter_id.return_value = "5001"
    principal.platform_mappings = {"improvedigital": {"advertiser_id": "5001"}}
    return principal


@pytest.fixture
def sample_request():
    return make_sample_create_request()


@pytest.fixture
def sample_packages():
    return [make_sample_video_package()]


def make_dry_run_adapter(mock_principal, config: dict | None = None) -> ImproveDigitalAdapter:
    return ImproveDigitalAdapter(
        config=config or {},
        principal=mock_principal,
        dry_run=True,
        tenant_id="tenant_impd_1",
    )


class TestRegistry:
    def test_get_adapter_schemas_returns_improvedigital_classes(self):
        schemas = get_adapter_schemas("improvedigital")
        assert schemas is not None
        assert schemas.connection_config is ImproveDigitalConnectionConfig
        assert schemas.product_config is ImproveDigitalProductConfig
        assert schemas.capabilities.inventory_entity_label == "Placements"

    def test_sync_capabilities_match_implementation_state(self):
        # Inventory sync landed with Phase 2; reporting sync landed with the
        # Report API cache (run_reporting_sync → improvedigital_line_item_stats).
        schemas = get_adapter_schemas("improvedigital")
        assert schemas.capabilities.supports_inventory_sync is True
        assert schemas.capabilities.supports_reporting_sync is True

    def test_default_channels_cover_classic_media_types(self):
        channels = get_adapter_default_channels("improvedigital")
        assert "display" in channels
        assert "olv" in channels


class TestReferenceNumbers:
    START, END = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 31, tzinfo=UTC)

    def test_payloads_carry_references_from_principal_mapping(self, mock_principal):
        mock_principal.platform_mappings = {
            "improvedigital": {
                "advertiser_id": "5001",
                "campaign_reference_number": "nike",
                "line_item_reference_number": "nike",
            }
        }
        adapter = make_dry_run_adapter(mock_principal)
        campaign = adapter._campaign_payload("adcp_PO-1", self.START, self.END)
        # Classic CampaignDto key confirmed by Improve Digital: reference_number
        assert campaign["reference_number"] == "nike"
        assert "campaign_reference_number" not in campaign
        assert campaign["advertiserId"] == 5001
        line_item = adapter._line_item_payload(make_sample_video_package(), 10.0, "fixed", self.START, self.END)
        assert line_item["reference_number"] == "nike"

    def test_references_fall_back_to_principal_id_for_older_mappings(self, mock_principal):
        # Buyer agents admitted before the fields existed still book with a reference.
        adapter = make_dry_run_adapter(mock_principal)
        assert adapter.campaign_reference_number == "principal_impd_1"
        assert adapter.line_item_reference_number == "principal_impd_1"
        assert adapter._campaign_payload("adcp_PO-1", self.START, self.END)["reference_number"] == "principal_impd_1"

    def _live_adapter_with_mock_client(self, mock_principal):
        adapter = make_dry_run_adapter(mock_principal)
        adapter._client = MagicMock()
        return adapter

    def test_campaign_reference_is_put_after_create(self, mock_principal):
        # Interim path agreed with Improve Digital: GET + PUT the campaign DTO, always.
        adapter = self._live_adapter_with_mock_client(mock_principal)
        adapter._client.campaigns.get_campaign.return_value = {"id": 1, "name": "adcp_PO-1", "reference_number": None}
        adapter._client.campaigns.update_campaign.return_value = {"id": 1, "reference_number": "principal_impd_1"}
        adapter._put_campaign_reference(1)
        adapter._client.campaigns.get_campaign.assert_called_once_with(1)
        cid, dto = adapter._client.campaigns.update_campaign.call_args.args
        assert cid == 1
        assert dto["reference_number"] == "principal_impd_1"
        assert dto["name"] == "adcp_PO-1"  # full DTO round-trips

    def test_campaign_reference_raises_when_put_does_not_persist(self, mock_principal):
        adapter = self._live_adapter_with_mock_client(mock_principal)
        adapter._client.campaigns.get_campaign.return_value = {"id": 1}
        adapter._client.campaigns.update_campaign.return_value = {"id": 1, "reference_number": None}
        with pytest.raises(ValueError, match="campaign 1 did not persist reference_number"):
            adapter._put_campaign_reference(1)

    def test_line_item_reference_is_put_after_create(self, mock_principal):
        adapter = self._live_adapter_with_mock_client(mock_principal)
        adapter._client.campaigns.get_line_item.return_value = {"id": 202, "name": "li", "budget": 12.0}
        adapter._client.campaigns.update_line_item.return_value = {"id": 202, "reference_number": "principal_impd_1"}
        adapter._put_line_item_reference(1, 202)
        adapter._client.campaigns.get_line_item.assert_called_once_with(1, 202)
        cid, lid, dto = adapter._client.campaigns.update_line_item.call_args.args
        assert (cid, lid) == (1, 202)
        assert dto["reference_number"] == "principal_impd_1"
        assert dto["budget"] == 12.0  # full DTO round-trips

    def test_line_item_reference_raises_when_put_does_not_persist(self, mock_principal):
        adapter = self._live_adapter_with_mock_client(mock_principal)
        adapter._client.campaigns.get_line_item.return_value = {"id": 202}
        adapter._client.campaigns.update_line_item.return_value = {"id": 202, "reference_number": None}
        with pytest.raises(ValueError, match="line item 202 .* did not persist reference_number"):
            adapter._put_line_item_reference(1, 202)


class TestAdapterConstruction:
    def test_dry_run_defers_client_construction(self, mock_principal):
        adapter = make_dry_run_adapter(mock_principal)
        assert adapter._client is None
        assert adapter.advertiser_id == "5001"

    def test_live_mode_without_credentials_raises(self, mock_principal):
        with pytest.raises(ValueError, match="client_id \\+ client_secret"):
            ImproveDigitalAdapter(
                config={"improve_demand_contact_id": 7},
                principal=mock_principal,
                dry_run=False,
                tenant_id="tenant_impd_1",
            )

    def test_live_mode_without_advertiser_constructs(self, mock_principal):
        # advertiserId is not part of the Classic campaign create schema
        # (sandbox-confirmed) — a missing advertiser mapping must not block.
        mock_principal.get_adapter_id.return_value = None
        adapter = ImproveDigitalAdapter(
            config={
                "client_id": "app-1",
                "client_secret": "s",
                "buying_entity_id": 421,
                "buying_entity_office_id": 635,
            },
            principal=mock_principal,
            dry_run=False,
            tenant_id="tenant_impd_1",
        )
        assert adapter.advertiser_id is None

    def test_live_mode_without_buying_entity_raises(self, mock_principal):
        # The Classic campaign API rejects campaigns without a buying entity
        # + office (sandbox-confirmed) — fail at construction, not at create.
        with pytest.raises(ValueError, match="buying_entity"):
            ImproveDigitalAdapter(
                config={"client_id": "app-1", "client_secret": "s"},
                principal=mock_principal,
                dry_run=False,
                tenant_id="tenant_impd_1",
            )

    def test_supported_pricing_models(self, mock_principal):
        adapter = make_dry_run_adapter(mock_principal)
        assert adapter.get_supported_pricing_models() == {"cpm"}

    def test_targeting_capabilities_reject_postal(self, mock_principal):
        capabilities = make_dry_run_adapter(mock_principal).get_targeting_capabilities()
        assert capabilities.geo_countries is True
        assert capabilities.geo_regions is True
        # 360Yield location targeting stops at city level — no postal systems.
        assert capabilities.us_zip is False
        assert capabilities.de_plz is False

    def test_creative_formats_cover_display_and_video(self, mock_principal):
        formats = make_dry_run_adapter(mock_principal).get_creative_formats()
        format_ids = {fmt["format_id"]["id"] for fmt in formats}
        assert "display_image" in format_ids
        assert "video_vast" in format_ids


class TestAdapterDryRun:
    def test_dry_run_creates_buy_without_calling_client(self, mock_principal, sample_request, sample_packages):
        adapter = make_dry_run_adapter(mock_principal, config={"improve_demand_contact_id": 7})
        response = invoke_create_media_buy(adapter, sample_request, sample_packages)
        assert response.packages is not None
        assert len(response.packages) == 1
        assert adapter._client is None

    def test_dry_run_rejects_postal_targeting(self, mock_principal, sample_request, sample_packages):
        postal = MagicMock()
        postal.geo_postal_areas = ["1012"]
        sample_packages[0].targeting_overlay = postal
        adapter = make_dry_run_adapter(mock_principal)
        response = invoke_create_media_buy(adapter, sample_request, sample_packages)
        assert response.errors[0].code == "unsupported_targeting"

    def test_dry_run_status_is_active(self, mock_principal):
        adapter = make_dry_run_adapter(mock_principal)
        response = adapter.check_media_buy_status("improvedigital_adcp_1", today=datetime.now(UTC))
        assert response.status == "active"

    def test_unsupported_update_action_rejected(self, mock_principal):
        adapter = make_dry_run_adapter(mock_principal)
        response = adapter.update_media_buy(
            "improvedigital_adcp_1",
            action="do_something_weird",
            package_id=None,
            budget=None,
            today=datetime.now(UTC),
        )
        assert response.errors

    def test_dry_run_inventory_sync_soft_fails(self, mock_principal):
        # No credentials in dry-run — the sync reports a failed run instead
        # of raising, so the shared scheduler records it as a failed SyncJob.
        adapter = make_dry_run_adapter(mock_principal)
        result = adapter.run_inventory_sync()
        assert result.succeeded is False
        assert "dry-run" in result.errors["adapter"]

    def test_pause_media_buy_dry_run_succeeds(self, mock_principal):
        adapter = make_dry_run_adapter(mock_principal)
        response = adapter.update_media_buy(
            "improvedigital_adcp_1",
            action="pause_media_buy",
            package_id=None,
            budget=None,
            today=datetime.now(UTC),
        )
        assert response.affected_packages == []


class TestClassicCreatives:
    def test_dry_run_tag_creative_approved(self, mock_principal):
        adapter = make_dry_run_adapter(mock_principal)
        statuses = adapter.add_creative_assets(
            "improvedigital_123",
            assets=[
                {
                    "creative_id": "cr_1",
                    "name": "Banner 300x250",
                    "asset_type": "banner",
                    "snippet": "<script>tag()</script>",
                    "width": 300,
                    "height": 250,
                    # CreativeDto requires advertiser_domain (derived from the
                    # click URL when not given) — validated live on dev.
                    "click_url": "https://brand.example.com/landing",
                }
            ],
            today=datetime.now(UTC),
        )
        assert statuses[0].status == "approved"
        assert statuses[0].creative_id == "cr_1"

    def test_asset_missing_size_rejected_explicitly(self, mock_principal):
        # CreativeDto requires a size — partial assets fail loudly, they are
        # never accepted and silently dropped later.
        adapter = make_dry_run_adapter(mock_principal)
        statuses = adapter.add_creative_assets(
            "improvedigital_123",
            assets=[{"creative_id": "cr_2", "snippet": "<script>tag()</script>"}],
            today=datetime.now(UTC),
        )
        assert statuses[0].status == "failed"
        assert "size" in statuses[0].message

    def test_dry_run_association_succeeds(self, mock_principal):
        adapter = make_dry_run_adapter(mock_principal)
        results = adapter.associate_creatives(["li_1"], ["9001"])
        assert results == [{"line_item_id": "li_1", "creative_id": "9001", "status": "success"}]
