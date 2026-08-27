"""Repository for the Improve Digital line-item stats cache.

Centralises tenant-scoped reads and bulk upserts of
``improvedigital_line_item_stats`` rows. Read paths feed
``ImproveDigitalAdapter.get_media_buy_delivery``; write paths feed the
Report API reporting sync (``src/adapters/improvedigital/reporting_sync.py``).

Core invariant: every query filters by ``tenant_id``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.core.database.models import ImproveDigitalLineItemStats


class ImproveDigitalLineItemStatsRepository:
    """Tenant-scoped access for the Improve Digital line-item stats cache."""

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def get_by_line_item_ids(self, line_item_ids: Iterable[str]) -> dict[str, ImproveDigitalLineItemStats]:
        """Return stats rows keyed by line_item_id. Missing line items are
        omitted (callers handle the absence as 'no data yet')."""
        ids = list(line_item_ids)
        if not ids:
            return {}
        stmt = select(ImproveDigitalLineItemStats).filter(
            ImproveDigitalLineItemStats.tenant_id == self._tenant_id,
            ImproveDigitalLineItemStats.line_item_id.in_(ids),
        )
        return {row.line_item_id: row for row in self._session.scalars(stmt).all()}

    def list_all(self) -> list[ImproveDigitalLineItemStats]:
        """Return every cached line-item stats row for this tenant, newest
        campaigns first. Feeds the admin reporting page."""
        stmt = (
            select(ImproveDigitalLineItemStats)
            .filter_by(tenant_id=self._tenant_id)
            .order_by(ImproveDigitalLineItemStats.campaign_id.desc(), ImproveDigitalLineItemStats.line_item_id)
        )
        return list(self._session.scalars(stmt).all())

    def list_by_campaign(self, campaign_id: str) -> list[ImproveDigitalLineItemStats]:
        """Return all cached line-item stats for one Classic campaign. Used by
        ``get_media_buy_delivery`` to aggregate totals across packages."""
        stmt = select(ImproveDigitalLineItemStats).filter_by(tenant_id=self._tenant_id, campaign_id=campaign_id)
        return list(self._session.scalars(stmt).all())

    def bulk_upsert(self, rows: Iterable[dict]) -> int:
        """Insert or update line-item stats rows. ``rows`` items must carry
        ``line_item_id``, ``impressions``, ``spend_micros``, ``as_of`` at
        minimum; ``tenant_id`` is forced to the repository's scope.

        Returns the number of rows touched.
        """
        payloads = [{**row, "tenant_id": self._tenant_id} for row in rows]
        if not payloads:
            return 0
        stmt = pg_insert(ImproveDigitalLineItemStats).values(payloads)
        update_cols = {
            col.name: stmt.excluded[col.name]
            for col in ImproveDigitalLineItemStats.__table__.columns
            if col.name not in ("tenant_id", "line_item_id")
        }
        stmt = stmt.on_conflict_do_update(index_elements=["tenant_id", "line_item_id"], set_=update_cols)
        result = self._session.execute(stmt)
        return getattr(result, "rowcount", 0) or 0

    def latest_sync_at(self) -> datetime | None:
        """Return the most recent ``last_synced_at`` across all cached line-item
        stats for this tenant, or ``None`` if the reporting sync has never run."""
        stmt = select(func.max(ImproveDigitalLineItemStats.last_synced_at)).filter_by(tenant_id=self._tenant_id)
        return self._session.scalar(stmt)
