"""Reporting sync for the Improve Digital adapter — Report API → stats cache.

Pulls definitive delivery metrics from the Improve Marketplace Report API
via ``POST /report/ext/preview`` (synchronous JSON, ≤500 rows) with
dimensions ``campaign_id, line_item_id`` and metrics ``impressions,
clicks, advertiser_payout, complete``, scoped with a ``campaign_id IN``
filter to this tenant's active Improve Digital media buys, and upserts
per-line-item rows (spend in micros) into the
``improvedigital_line_item_stats`` cache table.

Read paths (``ImproveDigitalAdapter.get_media_buy_delivery``) aggregate the
cache; they tolerate an empty cache by raising ``DeliveryDataUnavailable``
so the delivery webhook scheduler skips instead of reporting false zeros.

Tenants whose active buys exceed the 500-row preview limit need the async
``POST /report/ext/generation`` job — out of scope until a tenant actually
outgrows the preview path (the sync logs a loud warning when the cap is hit).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from src.adapters.improvedigital._buy_refs import campaign_id_from_refs, platform_order_ref
from src.adapters.improvedigital.client import ImproveDigitalClient, ImproveDigitalForbiddenError
from src.core.database.repositories.improvedigital_line_item_stats import (
    ImproveDigitalLineItemStatsRepository,
)
from src.core.database.repositories.media_buy import MediaBuyRepository

logger = logging.getLogger(__name__)

# Report API request vocabulary (Improve Marketplace Report API doc).
REPORT_TYPE = "EXT_CONSOLIDATE"
DIMENSIONS = ["campaign_id", "line_item_id"]
# advertiser_payout is spend in campaign currency; complete = video completions.
METRICS = ["impressions", "clicks", "advertiser_payout", "complete"]
PREVIEW_ROW_LIMIT = 500

# Report API currency dictionary (1=EUR, 2=USD, … per the Report API doc).
CURRENCY_IDS = {"EUR": 1, "USD": 2}


class ReportingScopeNotGranted(RuntimeError):
    """Raised when the Report API refuses this client (403) — the OAuth2
    client lacks the reporting role. Schedulers treat this as a soft,
    retryable state rather than exception spam."""

    def __init__(self) -> None:
        super().__init__(
            "Improve Digital Report API access is not granted for this OAuth2 "
            "client (403 on /report/ext/preview). Request the reporting role "
            "from the Improve Digital platform team."
        )


@dataclass
class ReportingSyncResult:
    """Summary of one reporting-sync run."""

    rows_updated: int
    campaigns_covered: int
    error: str | None = None


class ImproveDigitalReportingSync:
    """Drives the Report API preview → line-item stats cache flow.

    Composed by ``ImproveDigitalAdapter.run_reporting_sync`` (scheduler and
    admin "Sync Reporting Now") with a tenant-scoped client + DB session.
    """

    def __init__(
        self,
        client: ImproveDigitalClient,
        tenant_id: str,
        *,
        session: Session,
        currency: str = "EUR",
        timezone: str = "UTC",
        date_range_quick: str = "LAST_31_DAYS",
    ) -> None:
        self._client = client
        self._tenant_id = tenant_id
        self._session = session
        self._currency = currency
        self._timezone = timezone
        self._date_range_quick = date_range_quick

    def run(self, campaign_ids: list[str] | None = None) -> ReportingSyncResult:
        """Fetch metrics for the tenant's active campaigns and upsert the cache.

        Args:
            campaign_ids: Optional narrowing — if unset, enumerates the
                tenant's active media buys and reports on their campaigns.

        Raises:
            ReportingScopeNotGranted: Report API 403 for this client.
            ImproveDigitalError: other client-level failures.
        """
        ids = campaign_ids if campaign_ids is not None else self._active_campaign_ids()
        if not ids:
            logger.info("Improve Digital reporting sync tenant=%s: no active campaigns, nothing to do", self._tenant_id)
            return ReportingSyncResult(rows_updated=0, campaigns_covered=0)

        # Snake_case envelope + PREVIEW_REPORT action, as sent by the
        # platform UI (validated live on the dev platform — the camelCase
        # shape in the OpenAPI spec deserializes to a null request).
        payload = {
            "rows": PREVIEW_ROW_LIMIT,
            "report_generation_request": {
                "title": "",
                "report_type": REPORT_TYPE,
                "currency_id": CURRENCY_IDS.get(self._currency, 1),
                "date_range": {"quick": self._date_range_quick},
                "dimensions": list(DIMENSIONS),
                "metrics": list(METRICS),
                "filters": [{"column": "campaign_id", "operation": "IN", "value": [int(i) for i in ids]}],
                "timezone": self._timezone,
                "action": "PREVIEW_REPORT",
            },
        }
        logger.info(
            "Improve Digital reporting sync tenant=%s campaigns=%d window=%s",
            self._tenant_id,
            len(ids),
            self._date_range_quick,
        )
        try:
            response = self._client.reporting.preview(payload)
        except ImproveDigitalForbiddenError as exc:
            logger.info("Improve Digital reporting scope still pending for tenant=%s: %s", self._tenant_id, exc)
            raise ReportingScopeNotGranted() from exc

        rows = self._parse_rows(response)
        if len(rows) >= PREVIEW_ROW_LIMIT:
            logger.warning(
                "Improve Digital reporting preview returned %d rows (the preview cap) for tenant=%s — "
                "results may be truncated; the async /report/ext/generation path is needed for this tenant",
                len(rows),
                self._tenant_id,
            )
        updated = self._upsert_rows(rows)
        return ReportingSyncResult(rows_updated=updated, campaigns_covered=len(ids))

    # -- helpers --

    def _active_campaign_ids(self) -> list[str]:
        """Classic campaign IDs behind this tenant's active media buys.

        The adapter returns ``improvedigital_<campaign_id>`` as the buy ID;
        depending on the approval path it lands on ``media_buy_id`` directly
        or on the packages' ``platform_order_id`` (stamped by the core layer
        at create). Non-numeric candidates (e.g. HITL-internal IDs whose
        platform stamp hasn't happened yet) are skipped.
        """
        repo = MediaBuyRepository(self._session, self._tenant_id)
        buys = repo.get_active()
        packages_by_buy = repo.get_packages_for_ids([buy.media_buy_id for buy in buys]) if buys else {}
        ids: list[str] = []
        for buy in buys:
            campaign_id = campaign_id_from_refs(
                platform_order_ref(packages_by_buy.get(buy.media_buy_id)), buy.media_buy_id
            )
            if campaign_id is not None:
                ids.append(campaign_id)
        return sorted(set(ids))

    @staticmethod
    def _parse_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract per-line-item metric dicts from a ReportPreviewResponse.

        The preview returns ``{"columnOrder": [...], "rows": [...]}`` where
        each row is keyed by column name. Column labels are normalised
        (lowercase, spaces → underscores) so cosmetic label changes upstream
        don't break the sync.
        """
        rows = response.get("rows") or []
        parsed: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalised = {str(k).strip().lower().replace(" ", "_"): v for k, v in row.items()}
            line_item_id = normalised.get("line_item_id")
            if line_item_id in (None, ""):
                logger.warning("Skipping Improve Digital reporting row with no line_item_id: %s", row)
                continue
            parsed.append(normalised)
        return parsed

    def _upsert_rows(self, rows: list[dict[str, Any]]) -> int:
        as_of = datetime.now(UTC)
        payloads = []
        for row in rows:
            spend = _as_float(row.get("advertiser_payout"))
            payloads.append(
                {
                    "line_item_id": str(row["line_item_id"]),
                    "campaign_id": str(row["campaign_id"]) if row.get("campaign_id") not in (None, "") else None,
                    "impressions": _as_int(row.get("impressions")),
                    "clicks": _as_int(row.get("clicks")),
                    "completed_views": _as_int(row.get("complete")),
                    "spend_micros": int(round(spend * 1_000_000)) if spend is not None else 0,
                    "currency": self._currency,
                    "as_of": as_of,
                    "last_synced_at": as_of,
                }
            )
        repo = ImproveDigitalLineItemStatsRepository(self._session, self._tenant_id)
        repo.bulk_upsert(payloads)
        self._session.commit()
        return len(payloads)


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ImproveDigitalReportingSync",
    "ReportingScopeNotGranted",
    "ReportingSyncResult",
]
