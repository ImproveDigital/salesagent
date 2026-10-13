"""GAMReportingService routes currency-specific reports through the REST client."""

from unittest.mock import Mock

import pytest

from src.adapters.gam_reporting_service import GAMReportingService


class _FakeRestClient:
    """Records the definition it was asked for and returns canned flat rows."""

    def __init__(self, rows):
        self.rows = rows
        self.definition = None
        self.kwargs = None

    def build_historical_definition(self, dimensions, metrics, start_date, end_date, **kwargs):
        self.kwargs = kwargs
        self.definition = {"dimensions": list(dimensions), "metrics": list(metrics), **kwargs}
        return self.definition

    def run_report(self, definition, **_):
        assert definition is self.definition
        return self.rows


def _service(rest_client):
    client = Mock()
    return GAMReportingService(client, network_timezone="Europe/Amsterdam", rest_client=rest_client)


def test_currency_code_uses_rest_and_normalises_rows_to_soap_shape():
    rest = _FakeRestClient(
        [
            {
                "ADVERTISER_ID": 789,
                "ADVERTISER_NAME": "Adv",
                "ORDER_ID": 5104261184,
                "ORDER_NAME": "Order",
                "LINE_ITEM_ID": 111,
                "LINE_ITEM_NAME": "LI",
                "AD_SERVER_IMPRESSIONS": 1002,
                "AD_SERVER_CLICKS": 0,
                "AD_SERVER_REVENUE_WITHOUT_CPD": 1_020_000,  # AUD 1.02 in micros
                "AD_SERVER_COMPLETED_VIEWS": 0,
            }
        ]
    )
    service = _service(rest)

    data = service.get_reporting_data(
        date_range="all_time", order_id="5104261184", requested_timezone="America/New_York", currency_code="AUD"
    )

    assert rest.kwargs["currency_code"] == "AUD"
    assert rest.kwargs["order_id"] == "5104261184"
    assert rest.definition["metrics"] == [
        "AD_SERVER_IMPRESSIONS",
        "AD_SERVER_CLICKS",
        "AD_SERVER_REVENUE_WITHOUT_CPD",
        "AD_SERVER_COMPLETED_VIEWS",
    ]
    assert data.currency_code == "AUD"
    assert data.metrics["total_impressions"] == 1002
    assert data.metrics["total_spend"] == pytest.approx(1.02)
    assert data.data[0]["line_item_id"] == "111"
    assert data.data[0]["spend"] == pytest.approx(1.02)
    service.report_service.runReportJob.assert_not_called()


def test_currency_code_without_rest_client_is_explicit_error():
    service = _service(None)
    with pytest.raises(ValueError, match="no REST reporting client"):
        service.get_reporting_data(date_range="all_time", currency_code="AUD")


def test_no_currency_code_keeps_soap_path(monkeypatch):
    rest = _FakeRestClient([])
    service = _service(rest)
    monkeypatch.setattr(service, "_run_report", lambda report_job: [])

    data = service.get_reporting_data(date_range="all_time")

    assert data.currency_code is None
    assert rest.definition is None
