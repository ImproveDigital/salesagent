"""REPORTING_CURRENCY: one publisher-facing reporting currency per deployment.

Publisher pages (dashboard, reporting, media buy delivery) report in this
currency by asking the ad server for it — GAM ``reportCurrency``, Improve
Digital Report API ``currency_id`` — never by converting in the sales agent.
Ad servers that cannot report in another currency ignore it, and AdCP reads
(``report_in_buy_currency=True``) always use the buy currency instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

from src.adapters.gam_reporting_api import _reporting_service
from src.adapters.gam_reporting_service import GAMReportingService
from src.core.tenant_currency import publisher_reporting_currency, tenant_reporting_currency
from tests.unit.test_improvedigital_delivery_buy_currency import _FakeReporting, _run
from tests.unit.test_improvedigital_live_paths import make_live_adapter
from tests.unit.test_mcp_delivery_buy_currency import _adapter, _FakeReportingService, _run_adapter

_MOD = "src.core.tenant_currency"


# --- resolver --------------------------------------------------------------------


def test_setting_applies_only_to_ad_servers_that_can_report_in_it(monkeypatch):
    monkeypatch.setenv("REPORTING_CURRENCY", " eur ")

    assert publisher_reporting_currency("google_ad_manager") == "EUR"
    assert publisher_reporting_currency("improvedigital") == "EUR"
    assert publisher_reporting_currency("mock") is None
    assert publisher_reporting_currency(None) is None


def test_unset_setting_defaults_to_eur_and_empty_means_ad_server_currency(monkeypatch):
    monkeypatch.delenv("REPORTING_CURRENCY", raising=False)
    assert publisher_reporting_currency("google_ad_manager") == "EUR"

    monkeypatch.setenv("REPORTING_CURRENCY", "")
    assert publisher_reporting_currency("google_ad_manager") is None


def test_tenant_currency_prefers_setting_over_network_currency(monkeypatch):
    monkeypatch.setenv("REPORTING_CURRENCY", "EUR")
    adapter_config = MagicMock()
    adapter_config.adapter_type = "google_ad_manager"
    adapter_config.gam_network_currency = "USD"
    with (
        patch(f"{_MOD}.AdapterConfigRepository") as adapter_repo,
        patch(f"{_MOD}.CurrencyLimitRepository") as limit_repo,
    ):
        adapter_repo.return_value.find_by_tenant.return_value = adapter_config
        limit_repo.return_value.list_all.return_value = []

        assert tenant_reporting_currency(MagicMock(), "t1") == "EUR"


def test_tenant_currency_ignores_setting_for_mock_ad_server(monkeypatch):
    monkeypatch.setenv("REPORTING_CURRENCY", "EUR")
    adapter_config = MagicMock()
    adapter_config.adapter_type = "mock"
    adapter_config.gam_network_currency = None
    adapter_config.config_json = {}
    limit = MagicMock()
    limit.currency_code = "USD"
    with (
        patch(f"{_MOD}.AdapterConfigRepository") as adapter_repo,
        patch(f"{_MOD}.CurrencyLimitRepository") as limit_repo,
    ):
        adapter_repo.return_value.find_by_tenant.return_value = adapter_config
        limit_repo.return_value.list_all.return_value = [limit]

        assert tenant_reporting_currency(MagicMock(), "t1") == "USD"


# --- GAM -----------------------------------------------------------------------------


def test_gam_publisher_read_requests_the_setting_not_the_buy_currency(monkeypatch):
    monkeypatch.setenv("REPORTING_CURRENCY", "USD")
    service = _FakeReportingService()  # EUR network

    result = _run_adapter(_adapter([]), service, buy_currency="AUD", report_in_buy_currency=False)

    assert service.calls == ["USD"]
    assert result.currency == "USD"


def test_gam_publisher_read_in_network_currency_sends_no_report_currency(monkeypatch):
    monkeypatch.setenv("REPORTING_CURRENCY", "EUR")
    service = _FakeReportingService()  # EUR network

    result = _run_adapter(_adapter([]), service, buy_currency="AUD", report_in_buy_currency=False)

    assert service.calls == [None]
    assert result.currency == "EUR"


def test_gam_adcp_read_keeps_the_buy_currency_despite_the_setting(monkeypatch):
    monkeypatch.setenv("REPORTING_CURRENCY", "USD")
    service = _FakeReportingService()

    result = _run_adapter(_adapter([]), service, buy_currency="AUD")

    assert service.calls == ["AUD"]
    assert result.currency == "AUD"


def test_reporting_page_service_defaults_every_report_to_the_setting(monkeypatch):
    monkeypatch.setenv("REPORTING_CURRENCY", "EUR")
    now = datetime.now(UTC)

    service = _reporting_service(Mock(), "Europe/Amsterdam")
    query = service._build_report_query(["ORDER_ID"], now, now, order_id="1")["reportQuery"]
    explicit = service._build_report_query(["ORDER_ID"], now, now, order_id="1", currency_code="AUD")["reportQuery"]
    unset = GAMReportingService(Mock(), "Europe/Amsterdam")._build_report_query(["ORDER_ID"], now, now)["reportQuery"]

    assert query["reportCurrency"] == "EUR"
    assert explicit["reportCurrency"] == "AUD"
    assert "reportCurrency" not in unset


# --- Improve Digital --------------------------------------------------------------------


def test_improvedigital_publisher_read_uses_the_cache_the_sync_wrote_in_the_setting(monkeypatch):
    """The reporting sync runs in REPORTING_CURRENCY, so the cache already holds
    the setting currency and a publisher read never needs a live preview."""
    monkeypatch.setenv("REPORTING_CURRENCY", "USD")
    adapter = make_live_adapter()  # office currency EUR
    adapter._client.reporting = _FakeReporting()

    response, stats_repo = _run(adapter, "EUR", report_in_buy_currency=False, cache_currency="USD")

    assert adapter._client.reporting.payloads == []
    stats_repo.list_by_campaign.assert_called_once_with("101")
    assert response.currency == "USD"


def test_improvedigital_buyer_read_goes_live_when_buy_currency_is_not_the_setting(monkeypatch):
    """Cache is in the setting currency (USD); an EUR buy's AdCP read must ask the
    Report API for EUR instead of answering from USD rows."""
    monkeypatch.setenv("REPORTING_CURRENCY", "USD")
    adapter = make_live_adapter()
    adapter._client.reporting = _FakeReporting()

    response, stats_repo = _run(adapter, "EUR", cache_currency="USD")

    request = adapter._client.reporting.payloads[0]["report_generation_request"]
    assert request["currency_id"] == 1
    assert response.currency == "EUR"
    stats_repo.bulk_upsert.assert_not_called()


def test_improvedigital_setting_equal_to_office_currency_keeps_cache_path(monkeypatch):
    monkeypatch.setenv("REPORTING_CURRENCY", "EUR")
    adapter = make_live_adapter()
    adapter._client.reporting = _FakeReporting()

    response, stats_repo = _run(adapter, "EUR", report_in_buy_currency=False)

    assert adapter._client.reporting.payloads == []
    stats_repo.list_by_campaign.assert_called_once_with("101")
    assert response.currency == "EUR"


def test_improvedigital_reporting_sync_runs_in_the_setting_currency(monkeypatch):
    monkeypatch.setenv("REPORTING_CURRENCY", "USD")
    adapter = make_live_adapter()

    assert adapter.reporting_currency == "USD"
    monkeypatch.delenv("REPORTING_CURRENCY")
    assert adapter.reporting_currency == "EUR"


def test_improvedigital_sync_is_constructed_with_the_reporting_currency(monkeypatch):
    monkeypatch.setenv("REPORTING_CURRENCY", "USD")
    adapter = make_live_adapter()
    now = datetime.now(UTC)
    with (
        patch("src.core.database.database_session.get_db_session"),
        patch("src.adapters.improvedigital.reporting_sync.ImproveDigitalReportingSync") as sync_cls,
    ):
        sync_cls.return_value.earliest_start_for.return_value = None
        sync_cls.return_value.run.return_value = MagicMock(started_at=now, finished_at=now + timedelta(seconds=1))
        adapter.run_reporting_sync()

    assert sync_cls.call_args.kwargs["currency"] == "USD"
