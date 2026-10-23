"""Cost of one Improve Digital delivery read (the media buy page, an AdCP poll).

The database can sit behind a tunnel and the Report API preview is a
warehouse query, so a read must: load the buy currency once, never create a
server-side report job for an on-demand cache miss, and remember an empty
pull so repeated views of a buy that has not delivered yet stay cheap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import src.adapters.improvedigital.adapter as adapter_module
from src.adapters.base import DeliveryDataUnavailable
from src.core.schemas import ReportingPeriod
from tests.unit.test_improvedigital_live_paths import make_live_adapter

_PAGE_WINDOW = ReportingPeriod(start=datetime.now(UTC) - timedelta(days=3 * 365), end=datetime.now(UTC))


def _read(adapter, **kwargs):
    with (
        patch("src.core.database.database_session.get_db_session"),
        patch(
            "src.core.database.repositories.improvedigital_line_item_stats.ImproveDigitalLineItemStatsRepository"
        ) as stats_repo_cls,
        patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalReportingSync") as sync_cls,
    ):
        stats_repo_cls.return_value.list_by_campaign.return_value = []
        try:
            adapter.get_media_buy_delivery("mb_1", _PAGE_WINDOW, datetime.now(UTC), **kwargs)
        except DeliveryDataUnavailable:
            pass
    return sync_cls


def test_buy_currency_is_read_once_per_delivery_call(monkeypatch):
    adapter = make_live_adapter()
    monkeypatch.setattr(adapter, "_resolve_campaign_id", lambda _id: "101")
    with patch.object(adapter, "_media_buy_currency", return_value="SEK") as currency_read:
        _read(adapter, report_in_buy_currency=False)

    assert currency_read.call_count == 1


def test_on_demand_cache_miss_runs_preview_without_a_report_job(monkeypatch):
    monkeypatch.setenv("REPORTING_CURRENCY", "EUR")
    adapter_module._empty_delivery_pulls.clear()
    adapter = make_live_adapter()  # office EUR; the buy is SEK so the counters are skipped
    adapter._client.reporting = MagicMock()  # the fake client has no Report API surface by default
    monkeypatch.setattr(adapter, "_resolve_campaign_id", lambda _id: "101")
    monkeypatch.setattr(adapter, "_media_buy_currency", lambda _id: "SEK")

    sync_cls = _read(adapter, report_in_buy_currency=False)

    run_kwargs = sync_cls.return_value.run.call_args.kwargs
    assert run_kwargs["campaign_ids"] == ["101"]
    assert run_kwargs["update_media_buys"] is False
    assert run_kwargs["submit_generation"] is False


def test_empty_pull_is_remembered_so_the_next_page_view_skips_the_report_api(monkeypatch):
    monkeypatch.setenv("REPORTING_CURRENCY", "EUR")
    adapter_module._empty_delivery_pulls.clear()
    adapter = make_live_adapter()
    adapter._client.reporting = MagicMock()
    monkeypatch.setattr(adapter, "_resolve_campaign_id", lambda _id: "101")
    monkeypatch.setattr(adapter, "_media_buy_currency", lambda _id: "SEK")

    first = _read(adapter, report_in_buy_currency=False)
    second = _read(adapter, report_in_buy_currency=False)

    assert first.return_value.run.call_count == 1
    assert second.return_value.run.call_count == 0  # within the TTL: no second preview

    # Once the TTL has passed the pull runs again.
    key = (adapter.tenant_id or "default", "101")
    adapter_module._empty_delivery_pulls[key] -= adapter_module._EMPTY_PULL_TTL_SECONDS + 1
    third = _read(adapter, report_in_buy_currency=False)
    assert third.return_value.run.call_count == 1
