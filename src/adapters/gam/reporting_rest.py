"""Google Ad Manager REST Reporting API client.

The SOAP ``ReportService`` used by :mod:`src.adapters.gam_reporting_service`
always reports revenue in the network currency. The REST Reporting API accepts
``ReportDefinition.currencyCode`` and has GAM convert revenue into that
currency, provided it is the network currency or one of the network's
configured secondary currencies.

Workflow (https://developers.google.com/ad-manager/api/beta/reports):

1. ``POST /v1/networks/{code}/reports`` creates a hidden report resource.
2. ``POST /v1/{report.name}:run`` starts a long-running operation.
3. ``GET /v1/{operation.name}`` is polled until ``done``; the operation's
   ``response.reportResult`` names the result.
4. ``GET /v1/{reportResult}:fetchRows`` pages through the rows.

Rows come back as flat dicts keyed by the dimension and metric enum names of
the definition, with ``ReportValue`` wrappers decoded to Python values.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import requests

logger = logging.getLogger(__name__)

ADMANAGER_REST_BASE_URL = "https://admanager.googleapis.com/v1"
USER_AGENT = "AdCP-Sales-Agent/1.0"

# SOAP column name -> REST metric enum. The REST API has no
# AD_SERVER_CPM_AND_CPC_REVENUE; AD_SERVER_REVENUE_WITHOUT_CPD is the metric
# whose docs map it to the UI's "Ad server CPM and CPC revenue".
SOAP_TO_REST_METRICS: dict[str, str] = {
    "AD_SERVER_IMPRESSIONS": "AD_SERVER_IMPRESSIONS",
    "AD_SERVER_CLICKS": "AD_SERVER_CLICKS",
    "AD_SERVER_CPM_AND_CPC_REVENUE": "AD_SERVER_REVENUE_WITHOUT_CPD",
    "AD_SERVER_VIDEO_COMPLETIONS": "AD_SERVER_COMPLETED_VIEWS",
}

# Metrics with data format MONEY (see ``money_to_micros``).
MONEY_METRICS: frozenset[str] = frozenset({"AD_SERVER_REVENUE_WITHOUT_CPD"})

_FILTERABLE_ID_DIMENSIONS = ("ADVERTISER_ID", "ORDER_ID", "LINE_ITEM_ID")


class GAMRestReportError(Exception):
    """A REST report could not be created, run, or read."""


def decode_report_value(value: dict[str, Any] | None) -> Any:
    """Unwrap a ``ReportValue`` oneof into a Python value."""
    if not value:
        return None
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intListValue" in value:
        return [int(v) for v in value["intListValue"].get("values", [])]
    if "doubleListValue" in value:
        return [float(v) for v in value["doubleListValue"].get("values", [])]
    if "stringListValue" in value:
        return list(value["stringListValue"].get("values", []))
    if "bytesValue" in value:
        return value["bytesValue"]
    return None


def money_to_micros(value: Any) -> int:
    """Normalise a MONEY metric value to micros.

    GAM encodes money as micros everywhere else (SOAP ``Money``, the CSV
    report dump), so an integer is taken as micros. A float can only be
    currency units, since micros are integral, and is scaled up.
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value * 1_000_000)
    return int(value)


def _date_json(value: date) -> dict[str, int]:
    return {"year": value.year, "month": value.month, "day": value.day}


def _id_filter(dimension: str, raw_id: str) -> dict[str, Any]:
    return {
        "fieldFilter": {
            "field": {"dimension": dimension},
            "operation": "IN",
            "values": [{"intValue": str(int(raw_id))}],
        }
    }


class GAMRestReportClient:
    """Minimal client for ``admanager.googleapis.com/v1`` reports."""

    def __init__(
        self,
        network_code: str,
        auth_header_provider: Callable[[], dict[str, str]],
        *,
        session: requests.Session | None = None,
        base_url: str = ADMANAGER_REST_BASE_URL,
        poll_interval_seconds: float = 5.0,
        timeout_seconds: float = 600.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        """
        Args:
            network_code: GAM network code the reports are scoped to.
            auth_header_provider: Returns the ``Authorization`` header for each
                request (called per request so token refresh is the provider's job).
            session: HTTP session, injectable for tests.
            poll_interval_seconds / timeout_seconds: operation polling policy.
            sleep / clock: injectable for tests.
        """
        self.network_code = str(network_code)
        self._auth_header_provider = auth_header_provider
        self._session = session or requests.Session()
        self._base_url = base_url.rstrip("/")
        self._poll_interval = poll_interval_seconds
        self._timeout = timeout_seconds
        self._sleep = sleep
        self._clock = clock

    # -- definition -----------------------------------------------------

    def build_historical_definition(
        self,
        dimensions: list[str],
        metrics: list[str],
        start_date: date,
        end_date: date,
        *,
        currency_code: str | None = None,
        advertiser_id: str | None = None,
        order_id: str | None = None,
        line_item_id: str | None = None,
        time_zone_source: str = "PUBLISHER",
    ) -> dict[str, Any]:
        """Build a HISTORICAL ``ReportDefinition`` with ID filters and currency."""
        definition: dict[str, Any] = {
            "reportType": "HISTORICAL",
            "dimensions": list(dimensions),
            "metrics": list(metrics),
            "dateRange": {"fixed": {"startDate": _date_json(start_date), "endDate": _date_json(end_date)}},
            "timeZoneSource": time_zone_source,
        }
        if currency_code:
            definition["currencyCode"] = currency_code
        filters = []
        for dimension, raw_id in zip(_FILTERABLE_ID_DIMENSIONS, (advertiser_id, order_id, line_item_id), strict=True):
            if not raw_id:
                continue
            try:
                filters.append(_id_filter(dimension, raw_id))
            except (TypeError, ValueError):
                logger.warning("Ignoring non-numeric %s filter value %r", dimension, raw_id)
        if filters:
            definition["filters"] = filters
        return definition

    # -- execution ------------------------------------------------------

    def run_report(
        self, definition: dict[str, Any], *, display_name: str = "salesagent delivery"
    ) -> list[dict[str, Any]]:
        """Create, run and read a report. Returns flat rows keyed by enum name."""
        report = self._request(
            "POST",
            f"networks/{self.network_code}/reports",
            json_body={"displayName": display_name, "reportDefinition": definition},
        )
        report_name = report.get("name")
        if not report_name:
            raise GAMRestReportError(f"Report create returned no name: {report}")

        operation = self._request("POST", f"{report_name}:run")
        operation = self._wait_for_operation(operation)
        result_name = (operation.get("response") or {}).get("reportResult")
        if not result_name:
            raise GAMRestReportError(f"Report run completed without a reportResult: {operation}")

        rows: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"pageSize": 10000}
            if page_token:
                params["pageToken"] = page_token
            page = self._request("GET", f"{result_name}:fetchRows", params=params)
            rows.extend(self._decode_rows(page.get("rows", []), definition))
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        logger.info(
            "GAM REST report %s returned %d rows (currency=%s)",
            report_name,
            len(rows),
            definition.get("currencyCode") or "network",
        )
        return rows

    def _wait_for_operation(self, operation: dict[str, Any]) -> dict[str, Any]:
        operation_name = operation.get("name")
        if not operation_name:
            raise GAMRestReportError(f"Report run returned no operation name: {operation}")
        deadline = self._clock() + self._timeout
        while not operation.get("done"):
            if self._clock() >= deadline:
                raise GAMRestReportError(f"Report operation {operation_name} timed out after {self._timeout:.0f}s")
            self._sleep(self._poll_interval)
            operation = self._request("GET", operation_name)
        if operation.get("error"):
            raise GAMRestReportError(f"Report operation {operation_name} failed: {operation['error']}")
        return operation

    @staticmethod
    def _decode_rows(rows: list[dict[str, Any]], definition: dict[str, Any]) -> list[dict[str, Any]]:
        dimensions = definition.get("dimensions", [])
        metrics = definition.get("metrics", [])
        decoded = []
        for row in rows:
            out: dict[str, Any] = {}
            for name, value in zip(dimensions, row.get("dimensionValues", []), strict=False):
                out[name] = decode_report_value(value)
            groups = row.get("metricValueGroups") or [{}]
            for name, value in zip(metrics, groups[0].get("primaryValues", []), strict=False):
                out[name] = decode_report_value(value)
            decoded.append(out)
        return decoded

    # -- transport ------------------------------------------------------

    def _request(
        self,
        method: str,
        resource: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}/{resource}"
        headers = {"User-Agent": USER_AGENT, **self._auth_header_provider()}
        try:
            response = self._session.request(
                method, url, json=json_body, params=params, headers=headers, timeout=(30, 300)
            )
        except requests.RequestException as e:
            raise GAMRestReportError(f"{method} {resource} failed: {e}") from e
        if response.status_code >= 400:
            raise GAMRestReportError(f"{method} {resource} returned HTTP {response.status_code}: {response.text[:500]}")
        try:
            body = response.json()
        except ValueError as e:
            raise GAMRestReportError(f"{method} {resource} returned non-JSON body") from e
        return body if isinstance(body, dict) else {}
