"""Repository for the Improve Digital inventory cache.

Tenant-scoped reads and bulk upserts over ``improvedigital_inventory``.
Reads feed the Improve Digital adapter product-configuration UI; writes
come from :class:`ImproveDigitalInventorySync`.

Core invariant: every query filters by ``tenant_id``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.core.database.models import ImproveDigitalInventory


class ImproveDigitalInventoryRepository:
    """Tenant-scoped access for the Improve Digital inventory cache."""

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def list_by_type(
        self,
        entity_type: str,
        *,
        parent_id: str | None = None,
    ) -> list[ImproveDigitalInventory]:
        """Return cached rows of one entity_type, optionally filtered by parent.

        Placements carry their publisher as ``parent_id``; publishers,
        packages, and sizes are flat (``parent_id`` is NULL).
        """
        stmt = select(ImproveDigitalInventory).filter_by(tenant_id=self._tenant_id, entity_type=entity_type)
        if parent_id is not None:
            stmt = stmt.filter(ImproveDigitalInventory.parent_id == parent_id)
        return list(self._session.scalars(stmt).all())

    def _picker_filter(self, entity_type: str, *, parent_id: str | None, q: str | None):
        """Shared WHERE clause for the picker projections: tenant scope,
        entity_type, optional parent and optional case-insensitive substring
        match on name or entity_id (same semantics as :meth:`search`)."""
        stmt = select(
            ImproveDigitalInventory.entity_id,
            ImproveDigitalInventory.name,
            ImproveDigitalInventory.parent_id,
        ).filter_by(tenant_id=self._tenant_id, entity_type=entity_type)
        if parent_id is not None:
            stmt = stmt.filter(ImproveDigitalInventory.parent_id == parent_id)
        if q:
            pattern = f"%{q}%"
            stmt = stmt.where(
                (ImproveDigitalInventory.name.ilike(pattern)) | (ImproveDigitalInventory.entity_id.ilike(pattern))
            )
        return stmt

    def list_picker_rows(
        self,
        entity_type: str,
        *,
        parent_id: str | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[tuple[str, str | None, str | None]]:
        """Return ``(entity_id, name, parent_id)`` tuples for one entity_type,
        ordered by name then id so offset pagination is stable.

        Column projection, not ORM instances: the product-form pickers page
        through the placement set (tens to hundreds of thousands of rows) and
        only need these three fields. Loading ``ImproveDigitalInventory``
        objects here also decodes each row's ``raw_json`` JSONB payload and
        builds an ORM instance per row — measured at ~2 GB of resident
        memory per request in dev, which OOM-killed the 4 GB Fargate task
        whenever two product pages overlapped. Use :meth:`list_by_type`
        only when the raw payload is actually needed. ``limit=None`` returns
        every matching row (adapter-internal callers); the HTTP endpoint
        always passes a bounded limit.
        """
        stmt = self._picker_filter(entity_type, parent_id=parent_id, q=q).order_by(
            ImproveDigitalInventory.name.asc(), ImproveDigitalInventory.entity_id.asc()
        )
        if offset:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._session.execute(stmt).tuples().all())

    def count_picker_rows(self, entity_type: str, *, parent_id: str | None = None, q: str | None = None) -> int:
        """Total rows :meth:`list_picker_rows` would return without a limit —
        lets the picker show "N of M" and decide whether to offer Load more."""
        stmt = select(func.count()).select_from(self._picker_filter(entity_type, parent_id=parent_id, q=q).subquery())
        return int(self._session.scalar(stmt) or 0)

    def list_picker_rows_by_ids(self, entity_type: str, ids: Iterable[str]) -> list[tuple[str, str | None, str | None]]:
        """Resolve already-attached ids to ``(entity_id, name, parent_id)`` so
        the picker can label its chips without downloading the whole set."""
        wanted = [str(i) for i in ids if str(i).strip()]
        if not wanted:
            return []
        stmt = (
            select(
                ImproveDigitalInventory.entity_id,
                ImproveDigitalInventory.name,
                ImproveDigitalInventory.parent_id,
            )
            .filter_by(tenant_id=self._tenant_id, entity_type=entity_type)
            .where(ImproveDigitalInventory.entity_id.in_(wanted))
        )
        return list(self._session.execute(stmt).tuples().all())

    def search(
        self,
        entity_type: str,
        *,
        q: str | None = None,
        parent_id: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[ImproveDigitalInventory]:
        """Search cached inventory for product authoring surfaces."""
        stmt = select(ImproveDigitalInventory).filter_by(tenant_id=self._tenant_id, entity_type=entity_type)
        if parent_id is not None:
            stmt = stmt.filter(ImproveDigitalInventory.parent_id == parent_id)
        if q:
            pattern = f"%{q}%"
            stmt = stmt.where(
                (ImproveDigitalInventory.name.ilike(pattern)) | (ImproveDigitalInventory.entity_id.ilike(pattern))
            )
        stmt = (
            stmt.order_by(ImproveDigitalInventory.name.asc(), ImproveDigitalInventory.entity_id.asc())
            .offset(offset)
            .limit(limit)
        )
        return list(self._session.scalars(stmt).all())

    def bulk_upsert(self, rows: Iterable[dict]) -> int:
        """Insert or update inventory rows. ``rows`` items must carry
        ``entity_type``, ``entity_id``, ``raw_json`` at minimum;
        ``tenant_id`` is forced to the repository's scope.

        Returns the number of rows touched.
        """
        payloads = [{**row, "tenant_id": self._tenant_id} for row in rows]
        if not payloads:
            return 0
        stmt = pg_insert(ImproveDigitalInventory).values(payloads)
        update_cols = {
            col.name: stmt.excluded[col.name]
            for col in ImproveDigitalInventory.__table__.columns
            if col.name not in ("tenant_id", "entity_type", "entity_id")
        }
        stmt = stmt.on_conflict_do_update(index_elements=["tenant_id", "entity_type", "entity_id"], set_=update_cols)
        result = self._session.execute(stmt)
        return getattr(result, "rowcount", 0) or 0

    def latest_sync_at(self) -> datetime | None:
        """Return the most recent ``last_synced_at`` for this tenant, or
        ``None`` if the inventory sync has never run."""
        stmt = select(func.max(ImproveDigitalInventory.last_synced_at)).filter_by(tenant_id=self._tenant_id)
        return self._session.scalar(stmt)

    def counts_by_type(self) -> dict[str, int]:
        """Row counts per entity_type — feeds the browse page's quick stats
        without materializing tens of thousands of rows."""
        stmt = (
            select(ImproveDigitalInventory.entity_type, func.count())
            .filter_by(tenant_id=self._tenant_id)
            .group_by(ImproveDigitalInventory.entity_type)
        )
        return dict(self._session.execute(stmt).tuples().all())

    def delete_all(self) -> int:
        """Wipe the tenant's inventory cache. Used when an operator triggers
        a full resync via the admin UI."""
        stmt = delete(ImproveDigitalInventory).filter_by(tenant_id=self._tenant_id)
        result = self._session.execute(stmt)
        return getattr(result, "rowcount", 0) or 0
