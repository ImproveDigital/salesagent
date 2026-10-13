"""GAM delivery reports in the buy currency when the network can convert to it.

Buy in AUD on a EUR network with AUD as a secondary currency -> REST report in
AUD, labelled AUD. AUD not enabled -> SOAP network-currency figures, labelled
EUR. REST failure -> same honest fallback.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from src.adapters.gam.reporting_rest import GAMRestReportError
from src.adapters.gam_reporting_service import ReportingData
from src.adapters.google_ad_manager import GoogleAdManager
from src.core.schemas import Principal, ReportingPeriod


def _reporting_data(spend: float, currency_code: str | None) -> ReportingData:
    now = datetime.now(UTC)
    return ReportingData(
        data=[{"line_item_id": "111", "impressions": 1002, "clicks": 0, "spend": spend, "ctr": 0.0}],
        start_date=now,
        end_date=now,
        requested_timezone="America/New_York",
        data_timezone="Europe/Amsterdam",
        data_valid_until=now,
        query_type="all_time",
        dimensions=["LINE_ITEM_ID"],
        metrics={"total_impressions": 1002, "total_clicks": 0, "total_spend": spend, "average_ctr": 0.0},
        currency_code=currency_code,
    )


class _FakeReportingService:
    """Stands in for GAMReportingService; records currency_code per call."""

    def __init__(self, *, rest_fails: bool = False):
        self.calls: list[str | None] = []
        self.rest_fails = rest_fails
        self.network_currency = "EUR"

    def get_reporting_data(self, **kwargs):
        currency = kwargs.get("currency_code")
        self.calls.append(currency)
        if currency and self.rest_fails:
            raise GAMRestReportError("HTTP 403 insufficient scope")
        return _reporting_data(1.02 if currency == "AUD" else 0.62, currency)


def _adapter(secondary: list[str]) -> GoogleAdManager:
    principal = Mock(spec=Principal)
    principal.principal_id = "p"
    principal.platform_mappings = {}
    adapter = GoogleAdManager(
        config={
            "network_code": "123456",
            "refresh_token": "t",
            "enabled": True,
            "network_currency": "EUR",
            "secondary_currencies": secondary,
        },
        principal=principal,
        network_code="123456",
        advertiser_id="789",
        trafficker_id="1",
        dry_run=False,
        tenant_id="t1",
    )
    adapter.client = Mock()
    return adapter


def _run(adapter, service, buy_currency="AUD"):
    media_buy = Mock()
    media_buy.media_buy_id = "mb"
    media_buy.budget = 100.0
    media_buy.currency = buy_currency
    media_buy.external_id = "5104261184"
    media_buy.raw_request = {"packages": [{"package_id": "pkg_1", "platform_line_item_id": "111"}]}
    now = datetime.now(UTC)
    period = ReportingPeriod(start=now - timedelta(days=3 * 365), end=now)
    with (
        patch("src.core.database.database_session.get_db_session") as db,
        patch("src.adapters.gam_reporting_service.GAMReportingService", return_value=service) as service_cls,
    ):
        db.return_value.__enter__.return_value.scalars.return_value.first.return_value = media_buy
        result = adapter.get_media_buy_delivery("mb", period, now)
    return result, service_cls


def test_buy_currency_enabled_on_network_reports_in_buy_currency():
    service = _FakeReportingService()
    result, service_cls = _run(_adapter(["AUD", "GBP"]), service)

    assert service.calls == ["AUD"]
    assert result.currency == "AUD"
    assert result.totals.spend == 1.02
    assert result.by_package[0].spend == 1.02
    # The adapter hands the service a REST client so the currency path can run.
    assert service_cls.call_args.kwargs["rest_client"] is not None


def test_buy_currency_not_enabled_falls_back_to_network_currency():
    service = _FakeReportingService()
    result, _ = _run(_adapter(["GBP"]), service)

    assert service.calls == [None]
    assert result.currency == "EUR"
    assert result.totals.spend == 0.62


def test_buy_in_network_currency_uses_soap_path():
    service = _FakeReportingService()
    result, _ = _run(_adapter(["AUD"]), service, buy_currency="EUR")

    assert service.calls == [None]
    assert result.currency == "EUR"


def test_rest_failure_falls_back_to_network_currency_label():
    service = _FakeReportingService(rest_fails=True)
    result, _ = _run(_adapter(["AUD"]), service)

    assert service.calls == ["AUD", None]
    assert result.currency == "EUR"
    assert result.totals.spend == 0.62


def test_dry_run_has_no_rest_client():
    principal = Mock(spec=Principal)
    principal.principal_id = "p"
    principal.platform_mappings = {}
    adapter = GoogleAdManager(
        config={"network_code": "1", "enabled": True, "secondary_currencies": ["AUD"], "network_currency": "EUR"},
        principal=principal,
        network_code="1",
        dry_run=True,
        tenant_id="t1",
    )
    assert adapter._rest_report_client() is None
    assert adapter._delivery_report_currency("mb", "AUD", "EUR") is None
