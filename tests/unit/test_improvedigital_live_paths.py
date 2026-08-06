"""Live-mode wire behaviour of the Improve Digital adapter (fake client).

Exercises the Classic API call sequences with an in-memory fake client:
campaign + line-item creation with the platform datetime format, inventory
assignment envelopes, creative upload/binding, update actions, and the
reporting-cache read/write paths. No HTTP, no DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.adapters.base import DeliveryDataUnavailable
from src.adapters.improvedigital import ImproveDigitalAdapter
from src.adapters.improvedigital.client import ImproveDigitalForbiddenError
from src.adapters.improvedigital.reporting_sync import (
    ImproveDigitalReportingSync,
    ReportingScopeNotGranted,
)
from src.core.schemas import ReportingPeriod
from tests.helpers.adapter_test_helpers import (
    invoke_create_media_buy,
    make_sample_create_request,
    make_sample_video_package,
)

LIVE_CONFIG = {
    "client_id": "app-1",
    "client_secret": "s3cret",
    "improve_demand_contact_id": 17918,
    "buying_entity_id": 421,
    "business_unit_id": 33,
    "api_base_url": "https://api.360yielddev.example",
    "currency": "EUR",
    "timezone": "UTC",
}


class FakePrincipal:
    name = "classic_advertiser"
    principal_id = "principal_impd_1"
    platform_mappings = {"improvedigital": {"advertiser_id": "5001"}}

    def get_adapter_id(self, adapter: str) -> str | None:
        return "5001" if adapter == "improvedigital" else None


class FakeCampaignsClient:
    """Records Classic campaign/line-item calls and returns canned bodies."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._next_line_item_id = 202

    def create_campaign(self, payload):
        self.calls.append(("create_campaign", payload))
        return {**payload, "id": 101}

    def create_line_item(self, campaign_id, payload):
        self.calls.append(("create_line_item", campaign_id, payload))
        line_item_id = self._next_line_item_id
        self._next_line_item_id += 1
        return {**payload, "id": line_item_id}

    def set_line_item_placements(self, campaign_id, line_item_id, payload):
        self.calls.append(("set_line_item_placements", campaign_id, line_item_id, payload))
        return payload

    def set_packages(self, campaign_id, line_item_id, payload):
        self.calls.append(("set_packages", campaign_id, line_item_id, payload))
        return payload

    def list_line_items(self, campaign_id, **params):
        self.calls.append(("list_line_items", campaign_id))
        return {"line_items": [{"id": 202}, {"id": 203}]}

    def set_line_item_status(self, campaign_id, line_item_id, *, active):
        self.calls.append(("set_line_item_status", campaign_id, line_item_id, active))

    def get_line_item(self, campaign_id, line_item_id):
        self.calls.append(("get_line_item", campaign_id, line_item_id))
        return {"id": line_item_id, "name": "li", "start_date": "2026-08-04 10:00:00", "impression_cap": 1000}

    def update_line_item(self, campaign_id, line_item_id, payload):
        self.calls.append(("update_line_item", campaign_id, line_item_id, payload))
        return payload

    def archive_campaign(self, campaign_id):
        self.calls.append(("archive_campaign", campaign_id))

    def delete_campaign(self, campaign_id):
        self.calls.append(("delete_campaign", campaign_id))


class FakeCreativesClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def create_creative(self, campaign_id, payload):
        self.calls.append(("create_creative", campaign_id, payload))
        return {**payload, "id": 9001}

    def create_third_party_tag_creatives(self, campaign_id, creatives, **kwargs):
        self.calls.append(("create_third_party_tag_creatives", campaign_id, creatives))
        return [{**creative, "id": 9001} for creative in creatives]

    def set_line_item_creatives(self, campaign_id, line_item_id, payload):
        self.calls.append(("set_line_item_creatives", campaign_id, line_item_id, payload))
        return payload


class FakeLookupsClient:
    def sizes(self, **params):
        return {"sizes": [{"id": 4, "width": 300, "height": 250, "name": "300x250 (Medium Rectangle)"}]}


class FakeClient:
    def __init__(self) -> None:
        self.campaigns = FakeCampaignsClient()
        self.creatives = FakeCreativesClient()
        self.lookups = FakeLookupsClient()


def make_live_adapter() -> ImproveDigitalAdapter:
    adapter = ImproveDigitalAdapter(
        config=dict(LIVE_CONFIG),
        principal=FakePrincipal(),
        dry_run=False,
        tenant_id="tenant_impd_1",
    )
    adapter._client = FakeClient()
    return adapter


def make_targeted_package(package_id: str = "pkg_video_1"):
    package = make_sample_video_package(package_id)
    package.implementation_config = {"improvedigital": {"placement_ids": [11, 12], "package_ids": [77]}}
    return package


class TestCreateMediaBuyLive:
    def test_creates_campaign_and_line_items_with_platform_ids(self):
        adapter = make_live_adapter()
        response = invoke_create_media_buy(adapter, make_sample_create_request(), [make_targeted_package()])

        assert response.media_buy_id == "improvedigital_101"
        assert response.packages[0].platform_line_item_id == "202"
        assert response._platform_line_item_ids == {"pkg_video_1": "202"}

        calls = adapter._client.campaigns.calls
        campaign_payload = calls[0][1]
        assert campaign_payload["improve_demand_contact_id"] == 17918
        assert campaign_payload["buying_entity_id"] == 421
        assert campaign_payload["type"] == "Improve"
        # Platform datetime wire format — date-only strings are rejected upstream.
        assert len(campaign_payload["start_date"]) == 19
        assert campaign_payload["start_date"][10] == " "

        placements_call = next(c for c in calls if c[0] == "set_line_item_placements")
        assert placements_call[3] == {
            "line_item_placements": [{"id": 11, "assigned": True}, {"id": 12, "assigned": True}]
        }
        packages_call = next(c for c in calls if c[0] == "set_packages")
        assert packages_call[3] == {"line_item_packages": [{"id": 77, "assigned": True}]}

    def test_package_without_inventory_selection_fails_loudly(self):
        adapter = make_live_adapter()
        package = make_sample_video_package()
        package.implementation_config = {"improvedigital": {}}
        response = invoke_create_media_buy(adapter, make_sample_create_request(), [package])
        assert response.errors[0].code == "upstream_error"
        assert "no placement_ids or package_ids" in response.errors[0].message
        # The partially created campaign must not be left orphaned upstream.
        assert ("delete_campaign", 101) in adapter._client.campaigns.calls


class TestCreativesLive:
    def test_upload_echoes_platform_creative_id(self):
        adapter = make_live_adapter()
        statuses = adapter.add_creative_assets(
            "improvedigital_101",
            assets=[
                {
                    "creative_id": "cr_1",
                    "name": "Banner 300x250",
                    "asset_type": "banner",
                    "snippet": "<script>tag()</script>",
                    "width": 300,
                    "height": 250,
                    "advertiser_domain": "brand.example.com",
                }
            ],
            today=datetime.now(UTC),
        )
        assert statuses[0].status == "approved"
        assert statuses[0].creative_id == "9001"
        method, campaign_id, creatives = adapter._client.creatives.calls[0]
        assert method == "create_third_party_tag_creatives"
        assert campaign_id == 101
        body = creatives[0]
        assert body["size"] == "300x250 (Medium Rectangle)"
        assert body["size_id"] == 4
        assert body["advertiser_domain"] == "brand.example.com"
        assert body["third_party_type"] == "display"
        assert body["platform_types"] == ["Web"]
        assert "type" not in body

    def test_association_uses_campaign_scoped_binding(self):
        adapter = make_live_adapter()
        adapter._line_item_campaigns["202"] = 101
        results = adapter.associate_creatives(["202"], ["9001", "9002"])
        assert all(result["status"] == "success" for result in results)
        call = adapter._client.creatives.calls[0]
        assert call[:3] == ("set_line_item_creatives", 101, 202)
        assert call[3] == {"line_item_creatives": [{"id": 9001, "assigned": True}, {"id": 9002, "assigned": True}]}

    def test_association_without_campaign_mapping_fails(self):
        adapter = make_live_adapter()
        with patch.object(ImproveDigitalAdapter, "_campaign_id_for_line_item", return_value=None):
            results = adapter.associate_creatives(["999"], ["9001"])
        assert results[0]["status"] == "failed"
        assert "No Classic campaign" in results[0]["message"]


class TestUpdateMediaBuyLive:
    def test_pause_media_buy_pauses_every_line_item(self):
        adapter = make_live_adapter()
        response = adapter.update_media_buy(
            "improvedigital_101", action="pause_media_buy", package_id=None, budget=None, today=datetime.now(UTC)
        )
        assert [(p.package_id, p.paused) for p in response.affected_packages] == [("202", True), ("203", True)]
        status_calls = [c for c in adapter._client.campaigns.calls if c[0] == "set_line_item_status"]
        assert status_calls == [("set_line_item_status", 101, 202, False), ("set_line_item_status", 101, 203, False)]

    def test_update_package_impressions_read_modify_writes(self):
        adapter = make_live_adapter()
        with patch.object(ImproveDigitalAdapter, "_resolve_platform_line_item_id", return_value="202"):
            response = adapter.update_media_buy(
                "improvedigital_101",
                action="update_package_impressions",
                package_id="pkg_video_1",
                budget=2000,
                today=datetime.now(UTC),
            )
        assert [p.package_id for p in response.affected_packages] == ["pkg_video_1"]
        update_call = next(c for c in adapter._client.campaigns.calls if c[0] == "update_line_item")
        assert update_call[3]["impression_cap"] == 2000

    def test_pause_package_without_mapping_returns_typed_error(self):
        adapter = make_live_adapter()
        with patch.object(ImproveDigitalAdapter, "_resolve_platform_line_item_id", return_value=None):
            response = adapter.update_media_buy(
                "improvedigital_101",
                action="pause_package",
                package_id="pkg_video_1",
                budget=None,
                today=datetime.now(UTC),
            )
        assert response.errors[0].code == "missing_platform_id"

    def test_archive_order_archives_campaign(self):
        adapter = make_live_adapter()
        response = adapter.update_media_buy(
            "improvedigital_101", action="archive_order", package_id=None, budget=None, today=datetime.now(UTC)
        )
        assert not getattr(response, "errors", None)
        assert ("archive_campaign", 101) in adapter._client.campaigns.calls


class TestDeliveryLive:
    def _reporting_period(self) -> ReportingPeriod:
        start = datetime.now(UTC) - timedelta(days=7)
        return ReportingPeriod(start=start, end=start + timedelta(days=7))

    def test_empty_cache_raises_delivery_unavailable(self):
        adapter = make_live_adapter()
        with (
            patch("src.core.database.database_session.get_db_session"),
            patch(
                "src.core.database.repositories.improvedigital_line_item_stats.ImproveDigitalLineItemStatsRepository"
            ) as repo_cls,
        ):
            repo_cls.return_value.list_by_campaign.return_value = []
            with pytest.raises(DeliveryDataUnavailable):
                adapter.get_media_buy_delivery("improvedigital_101", self._reporting_period(), datetime.now(UTC))

    def test_cache_rows_aggregate_to_delivery_totals(self):
        adapter = make_live_adapter()
        rows = [
            SimpleNamespace(
                line_item_id="202",
                impressions=1000,
                clicks=10,
                completed_views=700,
                spend_micros=4_000_000,
                currency="EUR",
            ),
            SimpleNamespace(
                line_item_id="203",
                impressions=500,
                clicks=5,
                completed_views=100,
                spend_micros=2_000_000,
                currency="EUR",
            ),
        ]
        with (
            patch("src.core.database.database_session.get_db_session"),
            patch(
                "src.core.database.repositories.improvedigital_line_item_stats.ImproveDigitalLineItemStatsRepository"
            ) as repo_cls,
        ):
            repo_cls.return_value.list_by_campaign.return_value = rows
            response = adapter.get_media_buy_delivery("improvedigital_101", self._reporting_period(), datetime.now(UTC))
        assert response.totals.impressions == 1500
        assert response.totals.spend == 6.0
        assert {p.package_id for p in response.by_package} == {"202", "203"}
        assert response.currency == "EUR"


class TestReportingSyncParsing:
    def _syncer(self, client) -> ImproveDigitalReportingSync:
        return ImproveDigitalReportingSync(client, "tenant_impd_1", session=SimpleNamespace(commit=lambda: None))

    def test_parse_rows_normalises_column_labels(self):
        rows = ImproveDigitalReportingSync._parse_rows(
            {
                "rows": [
                    {"Campaign ID": 101, "Line Item ID": 202, "Impressions": "1000", "Advertiser Payout": "4.5"},
                    {"campaign_id": 101, "impressions": 10},  # no line item — skipped
                ]
            }
        )
        assert rows == [{"campaign_id": 101, "line_item_id": 202, "impressions": "1000", "advertiser_payout": "4.5"}]

    def test_run_upserts_spend_as_micros(self):
        upserted: list[list[dict]] = []

        class FakeReporting:
            def preview(self, payload):
                request = payload["report_generation_request"]
                assert request["filters"][0]["value"] == [101]
                assert request["report_type"] == "EXT_CONSOLIDATE"
                assert request["action"] == "PREVIEW_REPORT"
                return {
                    "rows": [
                        {
                            "campaign_id": 101,
                            "line_item_id": 202,
                            "impressions": 1000,
                            "clicks": 10,
                            "advertiser_payout": 4.5,
                            "complete": 700,
                        }
                    ]
                }

        client = SimpleNamespace(reporting=FakeReporting())
        syncer = self._syncer(client)
        with patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalLineItemStatsRepository") as repo_cls:
            repo_cls.return_value.bulk_upsert.side_effect = lambda rows: upserted.append(list(rows))
            result = syncer.run(campaign_ids=["101"])
        assert result.rows_updated == 1
        row = upserted[0][0]
        assert row["line_item_id"] == "202"
        assert row["spend_micros"] == 4_500_000
        assert row["completed_views"] == 700

    def test_forbidden_maps_to_scope_not_granted(self):
        class ForbiddenReporting:
            def preview(self, payload):
                raise ImproveDigitalForbiddenError("403")

        syncer = self._syncer(SimpleNamespace(reporting=ForbiddenReporting()))
        with pytest.raises(ReportingScopeNotGranted):
            syncer.run(campaign_ids=["101"])

    def test_no_active_campaigns_is_a_clean_noop(self):
        syncer = self._syncer(SimpleNamespace(reporting=None))
        result = syncer.run(campaign_ids=[])
        assert result.rows_updated == 0
        assert result.error is None
