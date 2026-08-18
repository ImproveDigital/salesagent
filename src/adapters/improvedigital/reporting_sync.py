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
outgrows the preview path (tracked in
docs/adapters/improvedigital/INTEGRATION_PLAN.md Phase 3).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from src.adapters.improvedigital.client import (
    ImproveDigitalClient,
    ImproveDigitalError,
    ImproveDigitalForbiddenError,
)
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
# ReportPreviewRequest.rows max per the OpenAPI spec (validated live 2026-08-18).
PREVIEW_ROW_LIMIT = 2000

# Reporting window bounds for the flight-aware relative date range: never
# narrower than the old LAST_31_DAYS behaviour, never wider than a year.
MIN_WINDOW_DAYS = 31
MAX_WINDOW_DAYS = 365

# Report API currency dictionary — the full live list from
# GET /report/ext/currency/available (2026-08-18). Consulted first;
# resolve_currency_id() only hits the endpoint for codes not listed here.
CURRENCY_IDS = {
    "EUR": 1,
    "USD": 2,
    "GBP": 3,
    "DKK": 4,
    "SEK": 5,
    "AUD": 6,
    "CZK": 7,
    "HUF": 8,
    "CHF": 9,
    "NOK": 10,
    "SGD": 11,
    "HKD": 12,
    "MYR": 13,
    "CAD": 14,
    "TRY": 15,
}

MEDIA_BUY_ID_PREFIX = "improvedigital_"


def campaign_id_for_buy(buy: Any) -> str | None:
    """Classic campaign id behind a media buy row, or None.

    The adapter returns ``improvedigital_<campaign_id>`` as the buy
    reference; depending on the approval path it lands on ``external_id``
    or ``media_buy_id``. Single home for the extraction idiom (also used
    by the admin reporting page and the adapter's ID resolution).
    """
    for candidate in (getattr(buy, "external_id", None), getattr(buy, "media_buy_id", None)):
        campaign_id = str(candidate or "").removeprefix(MEDIA_BUY_ID_PREFIX)
        if campaign_id.isdigit():
            return campaign_id
    return None


def resolve_currency_id(client: Any, currency: str | None) -> int:
    """Resolve a currency code to the Report API's numeric id.

    Static map first (covers the platform's full live dictionary as of
    2026-08-18 — no upstream round trip per sync); the live
    ``/report/ext/currency/available`` lookup only runs for codes the map
    doesn't know, so new upstream currencies work without a code change.
    """
    code = (currency or "EUR").strip().upper()
    if code in CURRENCY_IDS:
        return CURRENCY_IDS[code]
    lookup = getattr(getattr(client, "reporting", None), "available_currencies", None)
    if lookup is not None:
        try:
            for row in lookup() or []:
                if isinstance(row, dict) and str(row.get("code", "")).strip().upper() == code:
                    return int(row["id"])
        except (ImproveDigitalError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Improve Digital currency dictionary lookup failed for %r: %s", code, exc)
    logger.warning("Improve Digital Report API has no currency %r — falling back to EUR (id 1)", code)
    return 1


def quick_date_range(quick: str) -> dict[str, Any]:
    """A quick range as the wire actually accepts it.

    ``quick: TODAY`` 500s upstream on EXT_CONSOLIDATE (jOOQ cursor-null —
    the consolidated warehouse has no same-day partition; observed live
    2026-08-18, every other quick value works). A 1-day relative range
    answers the same question and works, so TODAY is translated here.
    """
    if quick == "TODAY":
        return {"relative": {"from_count": 1, "from_unit": "DAY", "to_count": 0, "to_unit": "DAY"}}
    return {"quick": quick}


def build_report_request(
    *, currency_id: int, date_range: dict[str, Any], campaign_ids: list[int], timezone: str, title: str = ""
) -> dict[str, Any]:
    """The EXT_CONSOLIDATE request body shared by the reporting sync and
    the reporting page's live date-range view — one home for the wire
    contract (snake_case body; the OpenAPI spec's camelCase deserializes
    to a null request, validated live)."""
    return {
        "title": title,
        "report_type": REPORT_TYPE,
        "currency_id": currency_id,
        "date_range": date_range,
        "dimensions": list(DIMENSIONS),
        "metrics": list(METRICS),
        "filters": [{"column": "campaign_id", "operation": "IN", "value": campaign_ids}],
        "timezone": timezone,
    }


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


class ReportingSyncNotImplemented(RuntimeError):
    """Retained for backwards compatibility with earlier stub imports."""

    def __init__(self) -> None:  # pragma: no cover - legacy shim
        super().__init__("Improve Digital reporting sync is implemented; this error is obsolete.")


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

    def run(
        self,
        campaign_ids: list[str] | None = None,
        *,
        earliest_start: date | None = None,
        update_media_buys: bool = True,
    ) -> ReportingSyncResult:
        """Fetch metrics for the tenant's active campaigns and upsert the cache.

        Flow (per the Improve Marketplace Report API doc): register the
        report via ``POST /report/ext/generation`` first (best-effort — it
        materialises the report server-side as an auditable job), then read
        the rows back synchronously with ``/report/ext/preview``, upsert the
        stats cache, and roll the totals up to the owning media buys'
        ``delivered_*`` columns.

        The window is always flight-aware (never a caller-picked partial
        range): the cache is keyed per line item with no time dimension and
        ``delivered_*`` are lifetime columns, so syncing a narrower window
        would overwrite lifetime totals with window-only counts.

        Args:
            campaign_ids: Optional narrowing — if unset, enumerates the
                tenant's active media buys and reports on their campaigns.
            earliest_start: Optional flight start for the reporting window —
                lets targeted runs (``campaign_ids`` given) keep a
                flight-aware window instead of the quick default; ignored
                in favour of the enumerated buys' own earliest start when
                ``campaign_ids`` is unset.
            update_media_buys: Roll totals up to ``delivered_*`` columns.
                Read paths that merely warm the cache pass False so a
                delivery *read* never mutates publisher-facing pacing data.

        Raises:
            ReportingScopeNotGranted: Report API 403 for this client.
            ImproveDigitalError: other client-level failures.
        """
        # Step-numbered logging so one grep ("reporting sync tenant=X") shows
        # the whole run in order: scope → window → generation → preview →
        # parse → cache upsert → delivered_* rollup.
        if campaign_ids is not None:
            ids = campaign_ids
            logger.info(
                "Improve Digital reporting sync tenant=%s [1/6] targeted run: campaigns=%s earliest_start=%s",
                self._tenant_id,
                ids,
                earliest_start,
            )
        else:
            ids, earliest_start = self._active_campaign_ids()
            logger.info(
                "Improve Digital reporting sync tenant=%s [1/6] enumerated %d active campaign(s) %s "
                "(earliest flight start %s)",
                self._tenant_id,
                len(ids),
                ids,
                earliest_start,
            )
        if not ids:
            logger.info("Improve Digital reporting sync tenant=%s: no active campaigns, nothing to do", self._tenant_id)
            return ReportingSyncResult(rows_updated=0, campaigns_covered=0)

        date_range = self._report_date_range(earliest_start)
        currency_id = self._currency_id()
        request_body = build_report_request(
            currency_id=currency_id,
            date_range=date_range,
            campaign_ids=[int(i) for i in ids],
            timezone=self._timezone,
            title="salesagent reporting sync",
        )
        logger.info(
            "Improve Digital reporting sync tenant=%s [2/6] window=%s timezone=%s currency=%s(id=%s) rows_cap=%d",
            self._tenant_id,
            date_range,
            self._timezone,
            self._currency,
            currency_id,
            PREVIEW_ROW_LIMIT,
        )
        self._submit_generation(request_body)  # logs [3/6] itself
        payload = {
            "rows": PREVIEW_ROW_LIMIT,
            "report_generation_request": {**request_body, "action": "PREVIEW_REPORT"},
        }
        started = time.monotonic()
        try:
            response = self._client.reporting.preview(payload)
        except ImproveDigitalForbiddenError as exc:
            logger.info("Improve Digital reporting scope still pending for tenant=%s: %s", self._tenant_id, exc)
            raise ReportingScopeNotGranted() from exc
        raw_count = len(response.get("rows") or []) if isinstance(response, dict) else 0
        logger.info(
            "Improve Digital reporting sync tenant=%s [4/6] POST /report/ext/preview -> %d raw row(s) in %.1fs",
            self._tenant_id,
            raw_count,
            time.monotonic() - started,
        )

        rows = self._parse_rows(response)
        logger.info(
            "Improve Digital reporting sync tenant=%s [5/6] parsed %d line-item row(s) (%d skipped)",
            self._tenant_id,
            len(rows),
            raw_count - len(rows),
        )
        if len(rows) >= PREVIEW_ROW_LIMIT:
            logger.warning(
                "Improve Digital reporting preview returned %d rows (the preview cap) for tenant=%s — "
                "results may be truncated; the async /report/ext/generation download path is needed for this tenant",
                len(rows),
                self._tenant_id,
            )
        updated = self._upsert_rows(rows)
        buys_updated = 0
        if update_media_buys:
            try:
                buys_updated = self._update_media_buy_delivery(rows)
            except Exception:
                # Denormalisation only — the cache (the read paths' source of
                # truth) is already committed; dashboards catch up next run.
                logger.warning(
                    "Improve Digital reporting sync tenant=%s: media buy delivered-columns rollup failed",
                    self._tenant_id,
                    exc_info=True,
                )
        logger.info(
            "Improve Digital reporting sync tenant=%s [6/6] upserted %d cache row(s), "
            "rolled delivered_* up to %d media buy(s)%s",
            self._tenant_id,
            updated,
            buys_updated,
            "" if update_media_buys else " (rollup skipped: cache-warming read)",
        )
        return ReportingSyncResult(rows_updated=updated, campaigns_covered=len(ids))

    def earliest_start_for(self, campaign_ids: list[str]) -> date | None:
        """Earliest flight start among the active buys behind the given
        campaigns — keeps targeted syncs (admin campaign filter) on the
        same flight-aware window as the full scheduler run, so they never
        shrink cached lifetime totals to the 31-day quick default."""
        wanted = {str(cid) for cid in campaign_ids}
        earliest: date | None = None
        repo = MediaBuyRepository(self._session, self._tenant_id)
        for buy in repo.get_active():
            if campaign_id_for_buy(buy) in wanted:
                start = getattr(buy, "start_date", None)
                if start and (earliest is None or start < earliest):
                    earliest = start
        return earliest

    def _submit_generation(self, request_body: dict[str, Any]) -> None:
        """Register the report as a generation job before previewing.

        Best-effort: the preview supplies the rows either way, so a
        generation failure is logged and never blocks the sync. Validated
        live 2026-08-18: snake_case body accepted, response carries
        ``report_generation_id`` + ``status_name: ENQUEUED``.
        """
        submit = getattr(self._client.reporting, "submit_generation", None)
        if submit is None:
            return
        try:
            status = submit(dict(request_body))
        except ImproveDigitalError as exc:
            logger.warning(
                "Improve Digital report generation submit failed for tenant=%s (continuing with preview): %s",
                self._tenant_id,
                exc,
            )
            return
        if isinstance(status, dict):
            logger.info(
                "Improve Digital reporting sync tenant=%s [3/6] POST /report/ext/generation -> id=%s status=%s",
                self._tenant_id,
                status.get("report_generation_id"),
                status.get("status_name"),
            )

    def _update_media_buy_delivery(self, rows: list[dict[str, Any]]) -> int:
        """Roll synced per-line-item rows up to the owning media buys'
        ``delivered_amount`` / ``delivered_impressions`` /
        ``delivery_synced_at`` columns (via the repository, mirroring the
        GAM rollup), so dashboards and pacing bars read fresh numbers
        without waiting for a details-page visit. Returns the number of
        buys updated."""
        from decimal import Decimal

        per_campaign: dict[str, dict[str, Any]] = {}
        for row in rows:
            campaign_id = str(row.get("campaign_id") or "")
            if not campaign_id:
                continue
            agg = per_campaign.setdefault(campaign_id, {"impressions": 0, "spend": 0.0})
            agg["impressions"] += _as_int(row.get("impressions")) or 0
            agg["spend"] += _as_float(row.get("advertiser_payout")) or 0.0
        if not per_campaign:
            return 0
        now = datetime.now(UTC)
        updated = 0
        repo = MediaBuyRepository(self._session, self._tenant_id)
        for buy in repo.get_active():
            buy_campaign = campaign_id_for_buy(buy)
            if buy_campaign is not None and buy_campaign in per_campaign:
                agg = per_campaign[buy_campaign]
                repo.update_fields(
                    buy.media_buy_id,
                    delivered_amount=Decimal(str(round(agg["spend"], 2))),
                    delivered_impressions=int(agg["impressions"]),
                    delivery_synced_at=now,
                )
                updated += 1
        self._session.commit()
        return updated

    # -- helpers --

    def _active_campaign_ids(self) -> tuple[list[str], date | None]:
        """Classic campaign IDs behind this tenant's active media buys,
        plus the earliest flight start among them (drives the reporting
        window so long-running buys keep their full delivery history).

        The adapter returns ``improvedigital_<campaign_id>`` as the buy ID;
        depending on the approval path it lands on ``media_buy_id`` or
        ``external_id``. Non-numeric candidates (e.g. HITL-internal IDs whose
        external stamp hasn't happened yet) are skipped.
        """
        repo = MediaBuyRepository(self._session, self._tenant_id)
        ids: list[str] = []
        earliest_start: date | None = None
        for buy in repo.get_active():
            campaign_id = campaign_id_for_buy(buy)
            if campaign_id is None:
                continue
            ids.append(campaign_id)
            # getattr keeps mypy out of the model's Mapped[Date]
            # annotation (a SQLAlchemy type object, not datetime.date).
            start = getattr(buy, "start_date", None)
            if start and (earliest_start is None or start < earliest_start):
                earliest_start = start
        return sorted(set(ids)), earliest_start

    def _report_date_range(self, earliest_start: date | None) -> dict[str, Any]:
        """Reporting window for the preview request.

        Flight-aware: a relative range spanning from the oldest active
        flight's start to now (validated live — ``fixed`` ranges 400/500 on
        the dev platform, ``relative`` works), clamped to
        [MIN_WINDOW_DAYS, MAX_WINDOW_DAYS]. Without a flight date (explicit
        ``campaign_ids``, or buys with no start), the configured quick
        window applies — the pre-flight-aware behaviour.
        """
        if earliest_start is None:
            return {"quick": self._date_range_quick}
        days = (datetime.now(UTC).date() - earliest_start).days + 1
        days = max(MIN_WINDOW_DAYS, min(days, MAX_WINDOW_DAYS))
        return {"relative": {"from_count": days, "from_unit": "DAY", "to_count": 0, "to_unit": "DAY"}}

    def _currency_id(self) -> int:
        return resolve_currency_id(self._client, self._currency)

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
    "ReportingSyncNotImplemented",
    "ReportingSyncResult",
]
