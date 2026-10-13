"""GAM REST Reporting API client: request shapes, polling, paging, decoding."""

from datetime import date

import pytest

from src.adapters.gam.reporting_rest import (
    GAMRestReportClient,
    GAMRestReportError,
    decode_report_value,
    money_to_micros,
)


class _Response:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _FakeSession:
    """Scripted HTTP session: answers (METHOD, url) in order and records calls."""

    def __init__(self, script: list[tuple[str, str, _Response]]):
        self.script = list(script)
        self.calls: list[dict] = []

    def request(self, method, url, json=None, params=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "params": params, "headers": headers})
        expected_method, expected_url, response = self.script.pop(0)
        assert (method, url) == (expected_method, expected_url), f"unexpected {method} {url}"
        return response


BASE = "https://admanager.googleapis.com/v1"
NET = "networks/1234"


def _client(session, **kwargs):
    sleeps: list[float] = []
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 1.0
        return clock["t"]

    client = GAMRestReportClient(
        "1234",
        lambda: {"Authorization": "Bearer tok"},
        session=session,
        sleep=sleeps.append,
        clock=tick,
        **kwargs,
    )
    return client, sleeps


def test_definition_carries_currency_fixed_dates_and_id_filters():
    client, _ = _client(_FakeSession([]))

    definition = client.build_historical_definition(
        ["DATE", "LINE_ITEM_ID"],
        ["AD_SERVER_IMPRESSIONS", "AD_SERVER_REVENUE_WITHOUT_CPD"],
        date(2026, 10, 1),
        date(2026, 10, 13),
        currency_code="AUD",
        advertiser_id="789",
        order_id="5104261184",
    )

    assert definition["reportType"] == "HISTORICAL"
    assert definition["timeZoneSource"] == "PUBLISHER"
    assert definition["currencyCode"] == "AUD"
    assert definition["dateRange"] == {
        "fixed": {"startDate": {"year": 2026, "month": 10, "day": 1}, "endDate": {"year": 2026, "month": 10, "day": 13}}
    }
    assert definition["filters"] == [
        {"fieldFilter": {"field": {"dimension": "ADVERTISER_ID"}, "operation": "IN", "values": [{"intValue": "789"}]}},
        {
            "fieldFilter": {
                "field": {"dimension": "ORDER_ID"},
                "operation": "IN",
                "values": [{"intValue": "5104261184"}],
            }
        },
    ]


def test_definition_without_currency_omits_currency_code():
    client, _ = _client(_FakeSession([]))
    definition = client.build_historical_definition(
        ["ORDER_ID"], ["AD_SERVER_CLICKS"], date(2026, 1, 1), date(2026, 1, 2)
    )
    assert "currencyCode" not in definition
    assert "filters" not in definition


def test_run_report_creates_runs_polls_and_pages_rows():
    definition = {"dimensions": ["LINE_ITEM_ID"], "metrics": ["AD_SERVER_IMPRESSIONS", "AD_SERVER_REVENUE_WITHOUT_CPD"]}
    report = f"{NET}/reports/55"
    op = f"{NET}/operations/reports/runs/9"
    result = f"{report}/results/77"
    session = _FakeSession(
        [
            ("POST", f"{BASE}/{NET}/reports", _Response(200, {"name": report, "reportId": "55"})),
            ("POST", f"{BASE}/{report}:run", _Response(200, {"name": op, "done": False})),
            ("GET", f"{BASE}/{op}", _Response(200, {"name": op, "done": False, "metadata": {"percentComplete": 50}})),
            ("GET", f"{BASE}/{op}", _Response(200, {"name": op, "done": True, "response": {"reportResult": result}})),
            (
                "GET",
                f"{BASE}/{result}:fetchRows",
                _Response(
                    200,
                    {
                        "rows": [
                            {
                                "dimensionValues": [{"intValue": "111"}],
                                "metricValueGroups": [
                                    {"primaryValues": [{"intValue": "1002"}, {"intValue": "620000"}]}
                                ],
                            }
                        ],
                        "nextPageToken": "p2",
                    },
                ),
            ),
            (
                "GET",
                f"{BASE}/{result}:fetchRows",
                _Response(
                    200,
                    {
                        "rows": [
                            {
                                "dimensionValues": [{"intValue": "222"}],
                                "metricValueGroups": [{"primaryValues": [{"intValue": "5"}, {"intValue": "0"}]}],
                            }
                        ]
                    },
                ),
            ),
        ]
    )
    client, sleeps = _client(session)

    rows = client.run_report(definition, display_name="poc")

    assert rows == [
        {"LINE_ITEM_ID": 111, "AD_SERVER_IMPRESSIONS": 1002, "AD_SERVER_REVENUE_WITHOUT_CPD": 620000},
        {"LINE_ITEM_ID": 222, "AD_SERVER_IMPRESSIONS": 5, "AD_SERVER_REVENUE_WITHOUT_CPD": 0},
    ]
    assert session.calls[0]["json"] == {"displayName": "poc", "reportDefinition": definition}
    assert all(c["headers"]["Authorization"] == "Bearer tok" for c in session.calls)
    assert session.calls[4]["params"] == {"pageSize": 10000}
    assert session.calls[5]["params"] == {"pageSize": 10000, "pageToken": "p2"}
    assert sleeps == [5.0, 5.0]


def test_failed_operation_raises():
    report = f"{NET}/reports/1"
    op = f"{NET}/operations/reports/runs/2"
    session = _FakeSession(
        [
            ("POST", f"{BASE}/{NET}/reports", _Response(200, {"name": report})),
            (
                "POST",
                f"{BASE}/{report}:run",
                _Response(200, {"name": op, "done": True, "error": {"code": 3, "message": "bad currency"}}),
            ),
        ]
    )
    client, _ = _client(session)
    with pytest.raises(GAMRestReportError, match="bad currency"):
        client.run_report({"dimensions": [], "metrics": []})


def test_http_error_raises_with_status():
    session = _FakeSession(
        [("POST", f"{BASE}/{NET}/reports", _Response(403, {"error": {"message": "insufficient scope"}}))]
    )
    client, _ = _client(session)
    with pytest.raises(GAMRestReportError, match="HTTP 403"):
        client.run_report({"dimensions": [], "metrics": []})


def test_operation_timeout_raises():
    report = f"{NET}/reports/1"
    op = f"{NET}/operations/reports/runs/2"
    pending = _Response(200, {"name": op, "done": False})
    session = _FakeSession(
        [
            ("POST", f"{BASE}/{NET}/reports", _Response(200, {"name": report})),
            ("POST", f"{BASE}/{report}:run", pending),
            ("GET", f"{BASE}/{op}", pending),
            ("GET", f"{BASE}/{op}", pending),
        ]
    )
    client, _ = _client(session, timeout_seconds=2.5)  # fake clock ticks 1s per read
    with pytest.raises(GAMRestReportError, match="timed out"):
        client.run_report({"dimensions": [], "metrics": []})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"intValue": "42"}, 42),
        ({"doubleValue": 1.5}, 1.5),
        ({"stringValue": "2026-10-13"}, "2026-10-13"),
        ({"boolValue": True}, True),
        ({"intListValue": {"values": ["1", "2"]}}, [1, 2]),
        ({}, None),
        (None, None),
    ],
)
def test_decode_report_value(value, expected):
    assert decode_report_value(value) == expected


@pytest.mark.parametrize(("value", "expected"), [(620000, 620000), (0.62, 620000), (None, 0), ("5", 5)])
def test_money_to_micros(value, expected):
    assert money_to_micros(value) == expected
