"""Reporting sync window and currency resolution (Report API OpenAPI spec).

Spec-derived behaviour validated live on the dev platform (2026-08-18):
- ReportPreviewRequest.rows max is 2000 (was assumed 500)
- relative date ranges work on the wire; fixed ranges 400/500
- /report/ext/currency/available serves the currency dictionary (15 rows)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from src.adapters.improvedigital.client import ImproveDigitalError
from src.adapters.improvedigital.reporting_sync import (
    MAX_WINDOW_DAYS,
    MIN_WINDOW_DAYS,
    PREVIEW_ROW_LIMIT,
    ImproveDigitalReportingSync,
)


class CapturingReporting:
    def __init__(self, currencies=None):
        self.payloads: list[dict] = []
        self._currencies = currencies

    def preview(self, payload):
        self.payloads.append(payload)
        return {"rows": []}

    def available_currencies(self):
        if isinstance(self._currencies, Exception):
            raise self._currencies
        return self._currencies


def make_syncer(reporting, currency="EUR") -> ImproveDigitalReportingSync:
    client = SimpleNamespace(reporting=reporting)
    return ImproveDigitalReportingSync(client, "t1", session=SimpleNamespace(commit=lambda: None), currency=currency)


def fake_buy(campaign_id: int, start_days_ago: int):
    return SimpleNamespace(
        external_id=f"improvedigital_{campaign_id}",
        media_buy_id=f"mb_{campaign_id}",
        start_date=(datetime.now(UTC) - timedelta(days=start_days_ago)).date(),
    )


def run_with_buys(syncer, buys):
    with (
        patch("src.adapters.improvedigital.reporting_sync.MediaBuyRepository") as repo_cls,
        patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalLineItemStatsRepository"),
    ):
        repo_cls.return_value.get_active.return_value = buys
        return syncer.run()


class TestFlightAwareWindow:
    def test_window_spans_the_oldest_active_flight(self):
        reporting = CapturingReporting(currencies=[])
        run_with_buys(make_syncer(reporting), [fake_buy(101, 100), fake_buy(102, 5)])

        date_range = reporting.payloads[0]["report_generation_request"]["date_range"]
        assert date_range == {"relative": {"from_count": 101, "from_unit": "DAY", "to_count": 0, "to_unit": "DAY"}}

    def test_window_clamps_to_min_and_max(self):
        reporting = CapturingReporting(currencies=[])
        run_with_buys(make_syncer(reporting), [fake_buy(101, 2)])
        assert reporting.payloads[0]["report_generation_request"]["date_range"]["relative"]["from_count"] == (
            MIN_WINDOW_DAYS
        )

        run_with_buys(make_syncer(reporting), [fake_buy(101, 900)])
        assert reporting.payloads[1]["report_generation_request"]["date_range"]["relative"]["from_count"] == (
            MAX_WINDOW_DAYS
        )

    def test_explicit_campaign_ids_keep_the_quick_window(self):
        reporting = CapturingReporting(currencies=[])
        syncer = make_syncer(reporting)
        with patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalLineItemStatsRepository"):
            syncer.run(campaign_ids=["101"])
        assert reporting.payloads[0]["report_generation_request"]["date_range"] == {"quick": "LAST_31_DAYS"}

    def test_preview_requests_the_spec_row_cap(self):
        reporting = CapturingReporting(currencies=[])
        syncer = make_syncer(reporting)
        with patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalLineItemStatsRepository"):
            syncer.run(campaign_ids=["101"])
        assert PREVIEW_ROW_LIMIT == 2000
        assert reporting.payloads[0]["rows"] == 2000


class TestTargetedRunWindow:
    def test_explicit_ids_with_earliest_start_use_flight_window(self):
        reporting = CapturingReporting(currencies=[])
        syncer = make_syncer(reporting)
        start = (datetime.now(UTC) - timedelta(days=100)).date()
        with patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalLineItemStatsRepository"):
            syncer.run(campaign_ids=["101"], earliest_start=start)
        assert reporting.payloads[0]["report_generation_request"]["date_range"] == {
            "relative": {"from_count": 101, "from_unit": "DAY", "to_count": 0, "to_unit": "DAY"}
        }


class TestDeliveryCacheMissFallback:
    """get_media_buy_delivery pulls the Report API live on a cache miss —
    the GAM-details-page behaviour — writing through the stats cache."""

    def _adapter(self):
        from src.adapters.improvedigital import ImproveDigitalAdapter

        class FakePrincipal:
            name = "p"
            principal_id = "p1"
            platform_mappings: dict = {}

            def get_adapter_id(self, adapter):
                return None

        adapter = ImproveDigitalAdapter(
            config={
                "client_id": "app-1",
                "client_secret": "s3cret",
                "buying_entity_id": 421,
                "buying_entity_office_id": 5068,
                "api_base_url": "https://api.360yielddev.example",
            },
            principal=FakePrincipal(),
            dry_run=False,
            tenant_id="t1",
        )
        adapter._client = SimpleNamespace(reporting=object())
        return adapter

    def _period(self, days: int = 100):
        from src.core.schemas import ReportingPeriod

        start = datetime.now(UTC) - timedelta(days=days)
        return ReportingPeriod(start=start, end=datetime.now(UTC))

    def test_classic_campaign_counters_answer_lifetime_requests(self):
        """The details page (3-year 'all-time' window) reads lifetime
        delivery straight off the Classic campaign entity — no cache, no
        Report API — mirroring GAM's live per-view query. Windowed
        requests (< 1 year) skip this path so lifetime counters are never
        mislabeled as a period's delivery."""
        adapter = self._adapter()
        adapter._client = SimpleNamespace(
            campaigns=SimpleNamespace(
                get_campaign=lambda cid: {"id": cid, "currency": "EUR"},
                list_line_items=lambda cid: {
                    "line_items": [
                        {"id": 202, "impressions": 31, "clicks": 2, "completes": 1, "spent": 0.06},
                        {"id": 203, "impressions": 9, "clicks": 0, "completes": 0, "spent": 0.01},
                    ]
                },
            ),
            reporting=object(),
        )
        response = adapter.get_media_buy_delivery("improvedigital_101", self._period(days=3 * 365), datetime.now(UTC))
        assert response.totals.impressions == 40
        assert response.totals.clicks == 2
        assert round(response.totals.spend, 2) == 0.07
        assert {p.package_id for p in response.by_package} == {"202", "203"}
        assert response.currency == "EUR"

    def test_windowed_requests_skip_the_lifetime_counters(self):
        """A 100-day window must not be answered with lifetime numbers —
        it goes to the report-backed cache instead."""
        adapter = self._adapter()
        campaign_reads: list[int] = []
        adapter._client = SimpleNamespace(
            campaigns=SimpleNamespace(
                get_campaign=lambda cid: campaign_reads.append(cid) or {"id": cid, "currency": "EUR"},
                list_line_items=lambda cid: {"line_items": [{"id": 202, "impressions": 999, "spent": 9.9}]},
            ),
            reporting=object(),
        )
        cached = [
            SimpleNamespace(
                line_item_id="202",
                impressions=10,
                clicks=1,
                completed_views=None,
                spend_micros=100_000,
                currency="EUR",
            )
        ]
        with (
            patch("src.core.database.database_session.get_db_session"),
            patch(
                "src.core.database.repositories.improvedigital_line_item_stats.ImproveDigitalLineItemStatsRepository"
            ) as repo_cls,
        ):
            repo_cls.return_value.list_by_campaign.return_value = cached
            response = adapter.get_media_buy_delivery("improvedigital_101", self._period(days=100), datetime.now(UTC))
        assert campaign_reads == []  # live path never touched
        assert response.totals.impressions == 10

    def test_cache_miss_triggers_targeted_pull_and_rereads(self):
        adapter = self._adapter()
        cached_after_pull = [
            SimpleNamespace(
                line_item_id="202",
                impressions=1000,
                clicks=10,
                completed_views=None,
                spend_micros=4_000_000,
                currency="EUR",
            )
        ]
        with (
            patch("src.core.database.database_session.get_db_session"),
            patch(
                "src.core.database.repositories.improvedigital_line_item_stats.ImproveDigitalLineItemStatsRepository"
            ) as repo_cls,
            patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalReportingSync") as sync_cls,
        ):
            repo_cls.return_value.list_by_campaign.side_effect = [[], cached_after_pull]
            response = adapter.get_media_buy_delivery("improvedigital_101", self._period(), datetime.now(UTC))

        run_kwargs = sync_cls.return_value.run.call_args
        assert run_kwargs.args == (["101"],) or run_kwargs.kwargs.get("campaign_ids") == ["101"]
        assert run_kwargs.kwargs["earliest_start"] == self._period().start.date()
        assert response.totals.impressions == 1000
        assert response.totals.clicks == 10

    def test_scope_pending_stays_soft_delivery_unavailable(self):
        from src.adapters.base import DeliveryDataUnavailable
        from src.adapters.improvedigital.reporting_sync import ReportingScopeNotGranted

        adapter = self._adapter()
        with (
            patch("src.core.database.database_session.get_db_session"),
            patch(
                "src.core.database.repositories.improvedigital_line_item_stats.ImproveDigitalLineItemStatsRepository"
            ) as repo_cls,
            patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalReportingSync") as sync_cls,
        ):
            repo_cls.return_value.list_by_campaign.return_value = []
            sync_cls.return_value.run.side_effect = ReportingScopeNotGranted()
            import pytest

            with pytest.raises(DeliveryDataUnavailable):
                adapter.get_media_buy_delivery("improvedigital_101", self._period(), datetime.now(UTC))


class TestGenerationFirstFlow:
    def test_generation_submitted_before_preview_with_same_request(self):
        calls: list[tuple[str, dict]] = []

        class Reporting:
            def submit_generation(self, body):
                calls.append(("generation", body))
                return {"report_generation_id": "gen-1", "status_name": "ENQUEUED"}

            def preview(self, payload):
                calls.append(("preview", payload))
                return {"rows": []}

            def available_currencies(self):
                return []

        syncer = make_syncer(Reporting())
        with patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalLineItemStatsRepository"):
            syncer.run(campaign_ids=["101"])

        assert [name for name, _ in calls] == ["generation", "preview"]
        gen_body = calls[0][1]
        preview_request = calls[1][1]["report_generation_request"]
        assert "action" not in gen_body  # generation takes the bare request
        assert preview_request["action"] == "PREVIEW_REPORT"
        assert gen_body["filters"] == preview_request["filters"]
        assert gen_body["date_range"] == preview_request["date_range"]

    def test_generation_failure_never_blocks_the_preview(self):
        class Reporting:
            def __init__(self):
                self.previewed = False

            def submit_generation(self, body):
                raise ImproveDigitalError("generation down")

            def preview(self, payload):
                self.previewed = True
                return {"rows": []}

            def available_currencies(self):
                return []

        reporting = Reporting()
        syncer = make_syncer(reporting)
        with patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalLineItemStatsRepository"):
            result = syncer.run(campaign_ids=["101"])
        assert reporting.previewed
        assert result.error is None

    def test_earliest_start_for_scopes_to_requested_campaigns(self):
        """Targeted syncs (admin campaign filter) derive a flight-aware
        window from the selected buys — never a partial window that would
        clobber lifetime cache totals."""
        syncer = make_syncer(CapturingReporting(currencies=[]))
        with patch("src.adapters.improvedigital.reporting_sync.MediaBuyRepository") as repo_cls:
            repo_cls.return_value.get_active.return_value = [fake_buy(101, 90), fake_buy(102, 300)]
            assert syncer.earliest_start_for(["101"]) == fake_buy(101, 90).start_date
            assert syncer.earliest_start_for(["101", "102"]) == fake_buy(102, 300).start_date
            assert syncer.earliest_start_for(["999"]) is None


class TestMediaBuyDeliveryRollup:
    def test_synced_rows_update_owning_buys_delivered_columns(self):
        from decimal import Decimal

        reporting = CapturingReporting(currencies=[])

        class FullReporting(CapturingReporting):
            def preview(self, payload):
                super().preview(payload)
                return {
                    "rows": [
                        {
                            "campaign_id": "101",
                            "line_item_id": "202",
                            "impressions": "1000",
                            "clicks": "10",
                            "advertiser_payout": "4.5",
                            "complete": "0",
                        },
                        {
                            "campaign_id": "101",
                            "line_item_id": "203",
                            "impressions": "500",
                            "clicks": "0",
                            "advertiser_payout": "1.5",
                            "complete": "0",
                        },
                    ]
                }

        buy = SimpleNamespace(
            external_id="improvedigital_101",
            media_buy_id="mb_x",
            delivered_amount=None,
            delivered_impressions=None,
            delivery_synced_at=None,
        )
        syncer = make_syncer(FullReporting(currencies=[]))
        with (
            patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalLineItemStatsRepository"),
            patch("src.adapters.improvedigital.reporting_sync.MediaBuyRepository") as buy_repo_cls,
        ):
            buy_repo_cls.return_value.get_active.return_value = [buy]
            result = syncer.run(campaign_ids=["101"])

        assert result.rows_updated == 2
        # Written through the repository (matching the GAM rollup), not raw ORM.
        update_call = buy_repo_cls.return_value.update_fields.call_args
        assert update_call.args == ("mb_x",)
        assert update_call.kwargs["delivered_amount"] == Decimal("6.0")
        assert update_call.kwargs["delivered_impressions"] == 1500
        assert update_call.kwargs["delivery_synced_at"] is not None

    def test_update_media_buys_false_skips_the_rollup(self):
        """Read paths that warm the cache must never mutate delivered_*."""

        class FullReporting(CapturingReporting):
            def preview(self, payload):
                super().preview(payload)
                return {
                    "rows": [
                        {
                            "campaign_id": "101",
                            "line_item_id": "202",
                            "impressions": "10",
                            "clicks": "0",
                            "advertiser_payout": "0.1",
                            "complete": "0",
                        }
                    ]
                }

        syncer = make_syncer(FullReporting(currencies=[]))
        with (
            patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalLineItemStatsRepository"),
            patch("src.adapters.improvedigital.reporting_sync.MediaBuyRepository") as buy_repo_cls,
        ):
            result = syncer.run(campaign_ids=["101"], update_media_buys=False)
        assert result.rows_updated == 1
        buy_repo_cls.return_value.update_fields.assert_not_called()


class TestEnvironmentGate:
    """The Report API warehouse serves foreign-environment campaign ids
    (observed live); the Classic campaign GET is env-scoped and gates
    which campaigns any report query may ask about."""

    def _client(self, existing: set[int]):
        from src.adapters.improvedigital.client import ImproveDigitalNotFoundError

        calls: list[int] = []

        def get_campaign(cid):
            calls.append(cid)
            if cid in existing:
                return {"id": cid}
            raise ImproveDigitalNotFoundError("Unknown Classic Campaign")

        return SimpleNamespace(campaigns=SimpleNamespace(get_campaign=get_campaign)), calls

    def test_foreign_campaigns_are_dropped(self):
        from src.adapters.improvedigital.reporting_sync import filter_to_existing_campaigns

        client, _ = self._client(existing={370320})
        kept = filter_to_existing_campaigns(client, "t-gate-1", ["314446", "370320"])
        assert kept == ["370320"]

    def test_verdicts_are_cached(self):
        from src.adapters.improvedigital.reporting_sync import filter_to_existing_campaigns

        client, calls = self._client(existing={370320})
        filter_to_existing_campaigns(client, "t-gate-2", ["314446", "370320"])
        filter_to_existing_campaigns(client, "t-gate-2", ["314446", "370320"])
        assert calls == [314446, 370320]  # second pass answered from cache

    def test_transient_errors_keep_the_campaign(self):
        from src.adapters.improvedigital.reporting_sync import filter_to_existing_campaigns

        def get_campaign(cid):
            raise ImproveDigitalError("upstream hiccup")

        client = SimpleNamespace(campaigns=SimpleNamespace(get_campaign=get_campaign))
        assert filter_to_existing_campaigns(client, "t-gate-3", ["370320"]) == ["370320"]

    def test_clients_without_campaign_surface_keep_everything(self):
        from src.adapters.improvedigital.reporting_sync import filter_to_existing_campaigns

        client = SimpleNamespace(reporting=object())  # test fakes
        assert filter_to_existing_campaigns(client, "t-gate-4", ["101", "102"]) == ["101", "102"]


class TestCurrencyResolution:
    def test_static_map_answers_known_codes_without_a_lookup(self):
        reporting = CapturingReporting(currencies=ImproveDigitalError("must not be called"))
        syncer = make_syncer(reporting, currency="GBP")
        with patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalLineItemStatsRepository"):
            syncer.run(campaign_ids=["101"])
        assert reporting.payloads[0]["report_generation_request"]["currency_id"] == 3

    def test_unknown_code_resolves_from_live_dictionary(self):
        reporting = CapturingReporting(currencies=[{"id": 99, "code": "XXX"}])
        syncer = make_syncer(reporting, currency="XXX")
        with patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalLineItemStatsRepository"):
            syncer.run(campaign_ids=["101"])
        assert reporting.payloads[0]["report_generation_request"]["currency_id"] == 99

    def test_falls_back_to_static_map_when_lookup_fails(self):
        reporting = CapturingReporting(currencies=ImproveDigitalError("boom"))
        syncer = make_syncer(reporting, currency="USD")
        with patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalLineItemStatsRepository"):
            syncer.run(campaign_ids=["101"])
        assert reporting.payloads[0]["report_generation_request"]["currency_id"] == 2

    def test_falls_back_when_client_lacks_the_method(self):
        class BareReporting:
            def __init__(self):
                self.payloads = []

            def preview(self, payload):
                self.payloads.append(payload)
                return {"rows": []}

        reporting = BareReporting()
        syncer = make_syncer(reporting, currency="EUR")
        with patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalLineItemStatsRepository"):
            syncer.run(campaign_ids=["101"])
        assert reporting.payloads[0]["report_generation_request"]["currency_id"] == 1
