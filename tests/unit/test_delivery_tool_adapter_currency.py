"""get_media_buy_delivery labels spend with the currency the adapter reported it in.

The adapter (GAM) says which currency its figures are in. The per-package
label and the response-level currency must follow that, not the pricing
option, so a buyer never reads EUR figures under an AUD label (or vice versa).
"""

from contextlib import ExitStack
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import (
    AdapterGetMediaBuyDeliveryResponse,
    AdapterPackageDelivery,
    DeliveryTotals,
    GetMediaBuyDeliveryRequest,
    ReportingPeriod,
)
from src.core.testing_hooks import AdCPTestContext
from src.core.tools.media_buy_delivery import _get_media_buy_delivery_impl

_MOD = "src.core.tools.media_buy_delivery"


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
            impressions=1002, spend=1.02, clicks=None, ctr=None, completed_views=None, completion_rate=None
        ),
        by_package=[AdapterPackageDelivery(package_id="pkg_1", impressions=1002, spend=1.02)],
        currency=currency,
    )


def _run(buy_currency: str, adapter_currency: str):
    identity = ResolvedIdentity(
        principal_id="test_principal",
        tenant_id="test_tenant",
        tenant={"tenant_id": "test_tenant"},
        protocol="mcp",
        testing_context=AdCPTestContext(dry_run=False, mock_time=None, jump_to_event=None, test_session_id=None),
    )
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.__exit__.return_value = False
    adapter = MagicMock()
    adapter.get_media_buy_delivery.return_value = _adapter_response(adapter_currency)
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_MOD}.get_principal_object", return_value=MagicMock()))
        stack.enter_context(patch(f"{_MOD}.get_adapter", return_value=adapter))
        stack.enter_context(patch(f"{_MOD}.MediaBuyUoW", return_value=uow))
        stack.enter_context(patch(f"{_MOD}._get_target_media_buys", return_value=[("mb_aud", _buy(buy_currency))]))
        stack.enter_context(patch(f"{_MOD}._get_pricing_options", return_value={}))
        stack.enter_context(patch(f"{_MOD}.unmaterialized_projected_ids", return_value=set()))
        stack.enter_context(patch(f"{_MOD}.DeliveryRepository"))
        return _get_media_buy_delivery_impl(GetMediaBuyDeliveryRequest(media_buy_ids=["mb_aud"]), identity)


def test_buy_currency_report_is_labelled_with_buy_currency():
    response = _run(buy_currency="AUD", adapter_currency="AUD")

    delivery = response.media_buy_deliveries[0]
    assert delivery.totals.spend == 1.02
    assert delivery.by_package[0].currency == "AUD"
    assert response.currency == "AUD"
    assert response.aggregated_totals.spend == 1.02


def test_network_currency_fallback_keeps_package_label_honest():
    response = _run(buy_currency="AUD", adapter_currency="EUR")

    delivery = response.media_buy_deliveries[0]
    # Package spend is a EUR figure, so the package says EUR. The response-level
    # currency keeps the existing buy-currency contract (see
    # test_delivery_response_currency.py); changing that is a separate decision.
    assert delivery.by_package[0].currency == "EUR"
    assert response.currency == "AUD"
