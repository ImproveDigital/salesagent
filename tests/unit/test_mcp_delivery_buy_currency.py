"""AdCP get_media_buy_delivery reports spend in the buy currency via GAM's ReportQuery.reportCurrency.

Buy booked in AUD on a EUR network with AUD as a secondary currency: the tool's
poll sets reportCurrency=AUD and labels AUD. Admin pages opt out and keep the
network currency. Nothing is converted by the sales agent.
"""

from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

from src.adapters.gam import build_gam_config_from_adapter
from src.adapters.gam_reporting_service import GAMReportingService, ReportingData
from src.adapters.google_ad_manager import GoogleAdManager
from src.core.schemas import (
    AdapterGetMediaBuyDeliveryResponse,
    AdapterPackageDelivery,
    DeliveryTotals,
    GetMediaBuyDeliveryRequest,
    Principal,
    ReportingPeriod,
)
from src.core.tools.media_buy_delivery import _get_media_buy_delivery_impl
from tests.factories import PrincipalFactory
from tests.unit.test_delivery_currency_network import _reporting_data as _network_reporting_data

_MOD = "src.core.tools.media_buy_delivery"


# --- reporting service -------------------------------------------------------


def test_report_query_sets_report_currency_only_when_asked():
    service = GAMReportingService(Mock(), network_timezone="Europe/Amsterdam")
    now = datetime.now()

    with_currency = service._build_report_query(["ORDER_ID"], now, now, order_id="1", currency_code="AUD")
    without = service._build_report_query(["ORDER_ID"], now, now, order_id="1")

    assert with_currency["reportQuery"]["reportCurrency"] == "AUD"
    assert "reportCurrency" not in without["reportQuery"]


def test_get_reporting_data_passes_currency_to_query_and_records_it(monkeypatch):
    service = GAMReportingService(Mock(), network_timezone="Europe/Amsterdam")
    seen: dict = {}

    def fake_run(report_job):
        seen.update(report_job["reportQuery"])
        return []

    monkeypatch.setattr(service, "_run_report", fake_run)

    data = service.get_reporting_data(date_range="all_time", order_id="1", currency_code="AUD")

    assert seen["reportCurrency"] == "AUD"
    assert data.currency_code == "AUD"


# --- GAM adapter ---------------------------------------------------------------


def _reporting_data(spend: float, currency_code: str | None) -> ReportingData:
    """The network-currency fixture, stamped with the reportCurrency used."""
    return replace(_network_reporting_data(spend), currency_code=currency_code)


class _FakeReportingService:
    """Stands in for GAMReportingService; records currency_code per call."""

    def __init__(self):
        self.calls: list[str | None] = []
        self.network_currency = "EUR"

    def get_reporting_data(self, **kwargs):
        currency = kwargs.get("currency_code")
        self.calls.append(currency)
        return _reporting_data(1.0 if currency == "AUD" else 0.62, currency)


def _adapter(secondary: list[str]) -> GoogleAdManager:
    principal = Mock(spec=Principal)
    principal.principal_id = "p"
    principal.platform_mappings = {}
    adapter = GoogleAdManager(
        config={
            "network_code": "1",
            "refresh_token": "t",
            "enabled": True,
            "network_currency": "EUR",
            "secondary_currencies": secondary,
        },
        principal=principal,
        network_code="1",
        advertiser_id="789",
        trafficker_id="1",
        dry_run=False,
        tenant_id="t1",
    )
    adapter.client = Mock()
    return adapter


def _run_adapter(adapter, service, buy_currency="AUD", **kwargs):
    media_buy = Mock()
    media_buy.media_buy_id = "mb"
    media_buy.budget = 100.0
    media_buy.currency = buy_currency
    media_buy.external_id = "4216426454"
    media_buy.raw_request = {"packages": [{"package_id": "pkg_1", "platform_line_item_id": "111"}]}
    now = datetime.now(UTC)
    period = ReportingPeriod(start=now - timedelta(days=3 * 365), end=now)
    with (
        patch("src.core.database.database_session.get_db_session") as db,
        patch("src.adapters.gam_reporting_service.GAMReportingService", return_value=service),
    ):
        db.return_value.__enter__.return_value.scalars.return_value.first.return_value = media_buy
        return adapter.get_media_buy_delivery("mb", period, now, **kwargs)


def test_default_requests_buy_currency_when_enabled_on_network():
    service = _FakeReportingService()
    result = _run_adapter(_adapter(["AUD", "USD"]), service)

    assert service.calls == ["AUD"]
    assert result.currency == "AUD"
    assert result.totals.spend == 1.0
    assert result.by_package[0].spend == 1.0


def test_admin_pages_opt_out_to_network_currency():
    service = _FakeReportingService()
    result = _run_adapter(_adapter(["AUD"]), service, report_in_buy_currency=False)

    assert service.calls == [None]
    assert result.currency == "EUR"
    assert result.totals.spend == 0.62


def test_buy_currency_requested_even_when_not_a_secondary_currency():
    """reportCurrency takes any currency GAM reports in (Help Center list), not
    only the network's trafficking (secondary) currencies."""
    service = _FakeReportingService()
    result = _run_adapter(_adapter(["USD"]), service)

    assert service.calls == ["AUD"]
    assert result.currency == "AUD"


class _RejectingReportingService(_FakeReportingService):
    """GAM refuses the requested reportCurrency; the network-currency retry works."""

    def get_reporting_data(self, **kwargs):
        currency = kwargs.get("currency_code")
        self.calls.append(currency)
        if currency:
            raise Exception("GAM report job failed")
        return _reporting_data(0.62, None)


def test_rejected_report_currency_falls_back_to_network_currency_and_says_so():
    service = _RejectingReportingService()
    result = _run_adapter(_adapter([]), service)

    assert service.calls == ["AUD", None]
    assert result.currency == "EUR"
    assert result.totals.spend == 0.62


def test_buy_in_network_currency_needs_no_report_currency():
    service = _FakeReportingService()
    result = _run_adapter(_adapter(["AUD"]), service, buy_currency="EUR")

    assert service.calls == [None]
    assert result.currency == "EUR"


def test_build_gam_config_carries_secondary_currencies():
    adapter_config = Mock()
    adapter_config.gam_network_code = "1"
    adapter_config.gam_trafficker_id = "1"
    adapter_config.gam_manual_approval_required = False
    adapter_config.gam_network_currency = "EUR"
    adapter_config.gam_secondary_currencies = ["AUD", "USD"]
    adapter_config.gam_service_account_json = None
    adapter_config.gam_refresh_token = "t"

    assert build_gam_config_from_adapter(adapter_config)["secondary_currencies"] == ["AUD", "USD"]


# --- AdCP tool -------------------------------------------------------------------


def _buy(currency: str) -> MagicMock:
    buy = MagicMock()
    buy.media_buy_id = "mb_aud"
    buy.currency = currency
    buy.budget = 100.0
    buy.start_date = date.today() - timedelta(days=10)
    buy.end_date = date.today() + timedelta(days=10)
    buy.status = "active"
    buy.is_paused = False
    buy.raw_request = {"packages": [{"package_id": "pkg_1", "product_id": "prod"}]}
    return buy


def _adapter_response(currency: str) -> AdapterGetMediaBuyDeliveryResponse:
    now = datetime.now(UTC)
    return AdapterGetMediaBuyDeliveryResponse(
        media_buy_id="mb_aud",
        reporting_period=ReportingPeriod(start=now - timedelta(days=30), end=now),
        totals=DeliveryTotals(
            impressions=1002, spend=1.0, clicks=None, ctr=None, completed_views=None, completion_rate=None
        ),
        by_package=[AdapterPackageDelivery(package_id="pkg_1", impressions=1002, spend=1.0)],
        currency=currency,
    )


def _run_tool(adapter_currency: str):
    identity = PrincipalFactory.make_identity(tenant={"tenant_id": "test_tenant"})
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.__exit__.return_value = False
    adapter = MagicMock()
    adapter.get_media_buy_delivery.return_value = _adapter_response(adapter_currency)
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_MOD}.get_principal_object", return_value=MagicMock()))
        stack.enter_context(patch(f"{_MOD}.get_adapter", return_value=adapter))
        stack.enter_context(patch(f"{_MOD}.MediaBuyUoW", return_value=uow))
        stack.enter_context(patch(f"{_MOD}._get_target_media_buys", return_value=[("mb_aud", _buy("AUD"))]))
        stack.enter_context(patch(f"{_MOD}._get_pricing_options", return_value={}))
        stack.enter_context(patch(f"{_MOD}.unmaterialized_projected_ids", return_value=set()))
        stack.enter_context(patch(f"{_MOD}.DeliveryRepository"))
        response = _get_media_buy_delivery_impl(GetMediaBuyDeliveryRequest(media_buy_ids=["mb_aud"]), identity)
    return response, adapter, uow


def test_tool_gets_buy_currency_by_default_and_does_not_touch_the_snapshot():
    response, adapter, uow = _run_tool("AUD")

    assert adapter.get_media_buy_delivery.call_args.kwargs.get("report_in_buy_currency", True) is True
    delivery = response.media_buy_deliveries[0]
    assert delivery.totals.spend == 1.0
    assert delivery.currency == "AUD"
    assert delivery.by_package[0].currency == "AUD"
    assert response.currency == "AUD"
    assert not any("delivered_amount" in c.kwargs for c in uow.media_buys.update_fields.call_args_list)


def test_tool_labels_follow_adapter_when_it_fell_back_to_network_currency():
    """Row, package and response-level currency all state what the figures are
    in (EUR), never the buy currency (AUD) the figures are not in."""
    response, _, _ = _run_tool("EUR")

    delivery = response.media_buy_deliveries[0]
    assert delivery.currency == "EUR"
    assert delivery.by_package[0].currency == "EUR"
    assert response.currency == "EUR"
    assert response.aggregated_totals.spend == 1.0
