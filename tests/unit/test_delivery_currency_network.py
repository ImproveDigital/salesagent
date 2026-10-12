"""GAM delivery spend must be labelled with the currency GAM reported it in.

The SOAP ReportService ``AD_SERVER_CPM_AND_CPC_REVENUE`` column is always
denominated in the network currency, regardless of the currency the buyer
booked the line items in. Stamping ``media_buy.currency`` on that figure
mislabels the spend (an AUD buy on a EUR network showed "AUD 0.62" for what
GAM reported as EUR 0.62).
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

from src.adapters.gam import build_gam_config_from_adapter
from src.adapters.gam_reporting_service import GAMReportingService, ReportingData
from src.adapters.google_ad_manager import GoogleAdManager
from src.core.schemas import Principal, ReportingPeriod


def _reporting_data(spend: float) -> ReportingData:
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
    )


def _adapter(config: dict) -> GoogleAdManager:
    principal = Mock(spec=Principal)
    principal.principal_id = "test_principal"
    principal.platform_mappings = {}
    adapter = GoogleAdManager(
        config=config,
        principal=principal,
        network_code="123456",
        advertiser_id="789",
        trafficker_id="101112",
        dry_run=False,
        tenant_id="test_tenant",
    )
    adapter.client = Mock()
    return adapter


def _media_buy(currency: str) -> Mock:
    media_buy = Mock()
    media_buy.media_buy_id = "mb_aud"
    media_buy.budget = 100.0
    media_buy.currency = currency
    media_buy.external_id = "5104261184"
    media_buy.raw_request = {"packages": [{"package_id": "pkg_1", "platform_line_item_id": "111"}]}
    return media_buy


def _run_delivery(adapter: GoogleAdManager, mock_db, mock_service_class, *, service_currency):
    mock_session = MagicMock()
    mock_db.return_value.__enter__.return_value = mock_session
    mock_session.scalars.return_value.first.return_value = _media_buy("AUD")

    service = Mock()
    service.network_currency = service_currency
    service.get_reporting_data.return_value = _reporting_data(0.62)
    mock_service_class.return_value = service

    now = datetime.now(UTC)
    period = ReportingPeriod(start=now - timedelta(days=3 * 365), end=now)
    return adapter.get_media_buy_delivery("mb_aud", period, now)


@patch("src.core.database.database_session.get_db_session")
@patch("src.adapters.gam_reporting_service.GAMReportingService")
def test_delivery_currency_is_configured_network_currency_not_buy_currency(mock_service_class, mock_db):
    adapter = _adapter({"network_code": "123456", "refresh_token": "t", "enabled": True, "network_currency": "EUR"})

    result = _run_delivery(adapter, mock_db, mock_service_class, service_currency=None)

    assert result.totals.spend == 0.62
    assert result.currency == "EUR"


@patch("src.core.database.database_session.get_db_session")
@patch("src.adapters.gam_reporting_service.GAMReportingService")
def test_delivery_currency_falls_back_to_live_network_currency(mock_service_class, mock_db):
    adapter = _adapter({"network_code": "123456", "refresh_token": "t", "enabled": True})

    result = _run_delivery(adapter, mock_db, mock_service_class, service_currency="EUR")

    assert result.currency == "EUR"


@patch("src.core.database.database_session.get_db_session")
@patch("src.adapters.gam_reporting_service.GAMReportingService")
def test_delivery_currency_last_resort_is_buy_currency(mock_service_class, mock_db):
    adapter = _adapter({"network_code": "123456", "refresh_token": "t", "enabled": True})

    result = _run_delivery(adapter, mock_db, mock_service_class, service_currency=None)

    assert result.currency == "AUD"


def test_reporting_service_captures_network_currency_from_current_network():
    client = Mock()
    network = Mock()
    network.timeZone = "Europe/Amsterdam"
    network.currencyCode = "EUR"
    client.GetService.return_value.getCurrentNetwork.return_value = network

    service = GAMReportingService(client)

    assert service.network_timezone == "Europe/Amsterdam"
    assert service.network_currency == "EUR"


def test_reporting_service_network_currency_is_none_when_lookup_fails():
    client = Mock()
    client.GetService.return_value.getCurrentNetwork.side_effect = RuntimeError("boom")

    service = GAMReportingService(client)

    assert service.network_currency is None


def test_build_gam_config_carries_network_currency():
    adapter_config = Mock()
    adapter_config.gam_network_code = "123456"
    adapter_config.gam_trafficker_id = "1"
    adapter_config.gam_manual_approval_required = False
    adapter_config.gam_network_currency = "EUR"
    adapter_config.gam_service_account_json = None
    adapter_config.gam_refresh_token = "t"

    assert build_gam_config_from_adapter(adapter_config)["network_currency"] == "EUR"
