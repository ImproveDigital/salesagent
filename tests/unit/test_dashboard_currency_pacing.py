"""Dashboard: the delivery snapshot is in the ad server's reporting currency.

Budgets are in the buy currency. When the two differ there is no FX in
salesagent, so pacing switches to impressions and the spend column is
labelled with the snapshot currency rather than the buy currency.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from src.admin.services.dashboard_service import DashboardService


def _buy(currency: str, *, delivered_amount, delivered_impressions=None, planned=None):
    buy = Mock()
    buy.media_buy_id = "mb_1"
    buy.advertiser_name = "Acme"
    buy.order_name = "Order"
    buy.currency = currency
    buy.budget = Decimal("100")
    buy.delivered_amount = delivered_amount
    buy.delivered_impressions = delivered_impressions
    buy.delivery_synced_at = None
    today = date.today()
    buy.start_date = today - timedelta(days=4)  # day 5 of 10 -> flight_pct 0.5
    buy.end_date = today + timedelta(days=5)
    package = Mock()
    package.package_config = {"impressions": planned} if planned is not None else {}
    buy.packages = [package]
    return buy


def _service(snapshot_currency: str | None) -> DashboardService:
    service = DashboardService("tenant_test")
    service._primary_currency_code = snapshot_currency
    return service


def test_same_currency_paces_on_spend():
    row = _service("EUR")._running_row(_buy("EUR", delivered_amount=Decimal("50")), datetime.now(UTC))

    assert row["pacing_basis"] == "spend"
    assert row["delivery_pct"] == 0.5
    assert row["pacing"] == "on-pace"


def test_buy_currency_differs_paces_on_impressions():
    buy = _buy("AUD", delivered_amount=Decimal("0.62"), delivered_impressions=1002, planned=10_000)

    row = _service("EUR")._running_row(buy, datetime.now(UTC))

    assert row["pacing_basis"] == "impressions"
    assert row["delivery_pct"] == 0.1
    assert row["pacing"] == "under"
    assert row["currency"] == "AUD"  # budget and weekly rate stay in the buy currency


def test_buy_currency_differs_without_planned_impressions_is_unknown():
    buy = _buy("AUD", delivered_amount=Decimal("0.62"), delivered_impressions=1002, planned=None)

    row = _service("EUR")._running_row(buy, datetime.now(UTC))

    assert row["pacing_basis"] == "impressions"
    assert row["pacing"] == "unknown"


def test_no_snapshot_currency_known_keeps_spend_pacing():
    row = _service(None)._running_row(_buy("AUD", delivered_amount=Decimal("50")), datetime.now(UTC))

    assert row["pacing_basis"] == "spend"


@patch("src.admin.services.dashboard_service.MediaBuyReadinessService")
@patch("src.admin.services.dashboard_service.MediaBuyRepository")
@patch("src.admin.services.dashboard_service.get_db_session")
def test_recent_buys_label_snapshot_spend_with_snapshot_currency(mock_db, mock_repo_cls, mock_readiness):
    synced = _buy("AUD", delivered_amount=Decimal("0.62"))
    synced.principal = None
    synced.created_at = datetime.now(UTC)
    estimated = _buy("AUD", delivered_amount=None)
    estimated.principal = None
    estimated.created_at = datetime.now(UTC)
    estimated.status = "active"
    mock_repo_cls.return_value.list_recent.return_value = [synced, estimated]
    mock_readiness.get_readiness_state.return_value = {"state": "live", "is_ready_to_activate": True}
    service = DashboardService("tenant_test")

    with patch.object(service, "_primary_currency", return_value="EUR"):
        rows = service.get_recent_media_buys()

    assert rows[0].spend_currency == "EUR"  # real snapshot: ad server currency
    assert rows[1].spend_currency == "AUD"  # calendar estimate from the AUD budget
