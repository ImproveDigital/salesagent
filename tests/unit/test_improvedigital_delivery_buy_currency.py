"""Improve Digital delivery in the buy currency via the Report API ``currency_id``.

The office fixes the campaign currency (EUR here) and every office-currency
source (campaign counters, the stats cache) reports in it. A buy booked in
another currency is read live from ``POST /report/ext/preview`` with that
currency's ``currency_id`` — converted upstream — and never written to the
cache. Publisher-facing callers (``report_in_buy_currency=False``) and any
failure of the live read keep the office currency, labelled as such.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from src.adapters.improvedigital.client import ImproveDigitalError
from src.adapters.improvedigital.reporting_sync import relative_date_range_from
from src.core.schemas import ReportingPeriod
from tests.unit.test_improvedigital_live_paths import make_live_adapter

_SYNC = "src.adapters.improvedigital.reporting_sync"
_CACHE_ROWS = [
    SimpleNamespace(
        line_item_id="202", impressions=1000, clicks=10, completed_views=None, spend_micros=4_000_000, currency="EUR"
    )
]


class _FakeReporting:
    """Records preview payloads; answers with one USD-denominated line-item row."""

    def __init__(self, *, fail: bool = False) -> None:
        self.payloads: list[dict] = []
        self.fail = fail

    def preview(self, payload):
        self.payloads.append(payload)
        if self.fail:
            raise ImproveDigitalError("403 reporting scope not granted")
        return {
            "column_order": [],
            "rows": [
                {
                    "campaign_id": 101,
                    "line_item_id": 202,
                    "impressions": 1000,
                    "clicks": 10,
                    "advertiser_payout": "4.35",
                }
            ],
        }


def _period(days: int = 30) -> ReportingPeriod:
    now = datetime.now(UTC)
    return ReportingPeriod(start=now - timedelta(days=days), end=now)


def _run(adapter, buy_currency: str, **kwargs):
    with (
        patch("src.core.database.database_session.get_db_session"),
        patch("src.core.database.repositories.media_buy.MediaBuyRepository") as buy_repo_cls,
        patch(
            "src.core.database.repositories.improvedigital_line_item_stats.ImproveDigitalLineItemStatsRepository"
        ) as stats_repo_cls,
        patch(f"{_SYNC}.filter_to_existing_campaigns", side_effect=lambda _c, _t, ids: list(ids)),
    ):
        buy_repo_cls.return_value.get_by_id.return_value = SimpleNamespace(
            external_id="improvedigital_101", currency=buy_currency
        )
        stats_repo_cls.return_value.list_by_campaign.return_value = list(_CACHE_ROWS)
        response = adapter.get_media_buy_delivery("mb_1", _period(), datetime.now(UTC), **kwargs)
    return response, stats_repo_cls.return_value


def test_buy_in_another_currency_is_read_live_in_that_currency():
    adapter = make_live_adapter()  # office currency EUR
    adapter._client.reporting = _FakeReporting()

    response, stats_repo = _run(adapter, "USD")

    request = adapter._client.reporting.payloads[0]["report_generation_request"]
    assert request["currency_id"] == 2  # USD in the Report API dictionary
    assert request["filters"] == [{"column": "campaign_id", "operation": "IN", "value": [101]}]
    assert request["date_range"]["relative"]["from_unit"] == "DAY"
    assert response.currency == "USD"
    assert response.totals.spend == 4.35
    assert response.by_package[0].package_id == "202"
    stats_repo.bulk_upsert.assert_not_called()  # never written to the office-currency cache
    stats_repo.list_by_campaign.assert_not_called()


def test_buy_in_office_currency_keeps_the_cache_path():
    adapter = make_live_adapter()
    adapter._client.reporting = _FakeReporting()

    response, stats_repo = _run(adapter, "EUR")

    assert adapter._client.reporting.payloads == []
    stats_repo.list_by_campaign.assert_called_once_with("101")
    assert response.currency == "EUR"
    assert response.totals.spend == 4.0


def test_publisher_facing_read_keeps_office_currency_for_a_foreign_currency_buy():
    adapter = make_live_adapter()
    adapter._client.reporting = _FakeReporting()

    response, _ = _run(adapter, "USD", report_in_buy_currency=False)

    assert adapter._client.reporting.payloads == []
    assert response.currency == "EUR"


def test_failed_live_read_falls_back_to_office_currency_honestly():
    adapter = make_live_adapter()
    adapter._client.reporting = _FakeReporting(fail=True)

    response, stats_repo = _run(adapter, "USD")

    assert len(adapter._client.reporting.payloads) == 1
    stats_repo.list_by_campaign.assert_called_once_with("101")
    assert response.currency == "EUR"
    assert response.totals.spend == 4.0


def test_relative_date_range_counts_days_from_window_start():
    today = datetime.now(UTC).date()

    assert relative_date_range_from(today - timedelta(days=29)) == {
        "relative": {"from_count": 30, "from_unit": "DAY", "to_count": 0, "to_unit": "DAY"}
    }
    assert relative_date_range_from(today)["relative"]["from_count"] == 1
    assert relative_date_range_from(today - timedelta(days=3 * 365))["relative"]["from_count"] == 365
