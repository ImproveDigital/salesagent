"""SyncJob repository — tenant-scoped access to the ``sync_jobs`` table.

Covers the reads and lifecycle writes the adapter sync orchestration
(``src/services/adapter_sync_orchestration.py``) needs. The pre-existing GAM
inventory path (``background_sync_service``) writes rows directly and
predates the repository layer; new sync services go through this repository
so SyncJob writes stay tenant-scoped and testable.

Core invariant: every query filters by ``tenant_id``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.database.models import SyncJob


class SyncJobRepository:
    """Tenant-scoped access for ``sync_jobs``."""

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def find_by_sync_id(self, sync_id: str) -> SyncJob | None:
        """Lookup a single SyncJob row for this tenant by sync_id.

        Returns ``None`` when the row doesn't exist OR belongs to another
        tenant — the tenant_id filter is enforced so the
        ``enqueue_adapter_sync`` async path can't accidentally transition
        another tenant's queued row to ``running``.
        """
        stmt = select(SyncJob).filter_by(sync_id=sync_id, tenant_id=self._tenant_id)
        return self._session.scalars(stmt).first()

    def latest_running_for_stream(self, *, adapter_type: str, sync_type: str) -> SyncJob | None:
        """Return the newest in-flight row for a tenant + adapter + stream."""
        stmt = (
            select(SyncJob)
            .where(
                SyncJob.tenant_id == self._tenant_id,
                SyncJob.adapter_type == adapter_type,
                SyncJob.sync_type == sync_type,
                SyncJob.status.in_(("pending", "queued", "running", "in_progress")),
            )
            .order_by(SyncJob.started_at.desc(), SyncJob.sync_id.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def create_job(
        self,
        *,
        sync_id: str,
        adapter_type: str,
        sync_type: str,
        status: str,
        triggered_by: str,
        triggered_by_id: str | None = None,
        started_at: datetime | None = None,
    ) -> SyncJob:
        """Create a SyncJob row (``queued`` for the async enqueue path,
        ``running`` for the synchronous orchestrator) scoped to this tenant."""
        job = SyncJob(
            sync_id=sync_id,
            tenant_id=self._tenant_id,
            adapter_type=adapter_type,
            sync_type=sync_type,
            status=status,
            started_at=started_at or datetime.now(UTC),
            triggered_by=triggered_by,
            triggered_by_id=triggered_by_id,
        )
        self._session.add(job)
        return job
