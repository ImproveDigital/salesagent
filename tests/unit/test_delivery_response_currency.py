"""get_media_buy_delivery response currency and aggregated spend.

The response-level ``currency`` used to be hardcoded to USD and
``aggregated_totals.spend`` summed buys regardless of currency.
"""

from contextlib import ExitStack
from datetime import UTC, date, datetime
from decimal import Decimal
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
from src.core.tools.media_buy_delivery import _aggregate_spend_by_currency, _get_media_buy_delivery_impl

_MOD = "src.core.tools.media_buy_delivery"


def test_single_currency_uses_buy_currency():
    currency, spend, error = _aggregate_spend_by_currency([("mb_1", "EUR", 2.0), ("mb_2", "EUR", 3.0)])

    assert (currency, spend, error) == ("EUR", 5.0, None)


def test_mixed_currency_aggregates_first_currency_and_warns():
    currency, spend, error = _aggregate_spend_by_currency(
        [("mb_1", "EUR", 2.0), ("mb_2", "USD", 100.0), ("mb_3", "EUR", 3.0)]
    )

    assert (currency, spend) == ("EUR", 5.0)
    assert error is not None
    assert error.code == "mixed_currency"
    assert "mb_2 (USD)" in error.message
    assert "mb_1" not in error.message


def test_no_deliveries_defaults_to_usd():
    assert _aggregate_spend_by_currency([]) == ("USD", 0.0, None)


def _buy(media_buy_id: str, currency: str) -> MagicMock:
    buy = MagicMock()
    buy.media_buy_id = media_buy_id
    buy.currency = currency
    buy.budget = Decimal("100")
    buy.start_date = date(2025, 1, 1)
    buy.end_date = date(2099, 12, 31)
    buy.is_paused = False
    buy.raw_request = {"packages": [{"package_id": f"pkg_{media_buy_id}", "product_id": "prod_1"}]}
    return buy


def _adapter_response(media_buy_id: str, **kwargs) -> AdapterGetMediaBuyDeliveryResponse:
    now = datetime.now(UTC)
    return AdapterGetMediaBuyDeliveryResponse(
        media_buy_id=media_buy_id,
        reporting_period=ReportingPeriod(start=now, end=now),
        totals=DeliveryTotals(impressions=1000, spend=kwargs["spend"]),
        by_package=[AdapterPackageDelivery(package_id=f"pkg_{media_buy_id}", impressions=1000, spend=kwargs["spend"])],
        currency=kwargs["currency"],
    )


def test_impl_reports_buy_currency_and_excludes_other_currencies_from_spend():
    buys = [("mb_eur", _buy("mb_eur", "EUR")), ("mb_usd", _buy("mb_usd", "USD"))]
    adapter = MagicMock()
    adapter.get_media_buy_delivery.side_effect = lambda media_buy_id, **_: _adapter_response(
        media_buy_id, spend=2.0 if media_buy_id == "mb_eur" else 100.0, currency="EUR"
    )
    delivery_repo = MagicMock()
    delivery_repo.get_max_sequence_number.return_value = 0
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

    with ExitStack() as stack:
        stack.enter_context(patch(f"{_MOD}.get_principal_object", return_value=MagicMock()))
        stack.enter_context(patch(f"{_MOD}.get_adapter", return_value=adapter))
        stack.enter_context(patch(f"{_MOD}.MediaBuyUoW", return_value=uow))
        stack.enter_context(patch(f"{_MOD}._get_target_media_buys", return_value=buys))
        stack.enter_context(patch(f"{_MOD}._get_pricing_options", return_value={}))
        stack.enter_context(patch(f"{_MOD}._is_circuit_breaker_open", return_value=False))
        stack.enter_context(patch(f"{_MOD}.DeliveryRepository", return_value=delivery_repo))
        response = _get_media_buy_delivery_impl(GetMediaBuyDeliveryRequest(), identity)

    assert response.currency == "EUR"
    assert response.aggregated_totals.spend == 2.0
    assert response.aggregated_totals.media_buy_count == 2
    assert [e.code for e in response.errors] == ["mixed_currency"]
