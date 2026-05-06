"""AdmittedOperator repository — tenant-scoped CRUD for admitted operators.

PR 1 of [signing-non-embedded](../../../../docs/design/signing-non-embedded.md).
Tenant-scoped at construction; every query filters by tenant_id.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.database.models import AdmittedOperator


class AdmittedOperatorRepository:
    """Tenant-scoped CRUD against ``admitted_operators``."""

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def list_active(self) -> list[AdmittedOperator]:
        stmt = (
            select(AdmittedOperator)
            .where(
                AdmittedOperator.tenant_id == self._tenant_id,
                AdmittedOperator.is_active.is_(True),
            )
            .order_by(AdmittedOperator.created_at)
        )
        return list(self._session.scalars(stmt).all())

    def list_all(self) -> list[AdmittedOperator]:
        stmt = (
            select(AdmittedOperator)
            .where(AdmittedOperator.tenant_id == self._tenant_id)
            .order_by(AdmittedOperator.created_at)
        )
        return list(self._session.scalars(stmt).all())

    def get_by_id(self, operator_id: str) -> AdmittedOperator | None:
        stmt = select(AdmittedOperator).where(
            AdmittedOperator.tenant_id == self._tenant_id,
            AdmittedOperator.operator_id == operator_id,
        )
        return self._session.scalars(stmt).first()

    def get_by_brand_json_url(self, brand_json_url: str) -> AdmittedOperator | None:
        stmt = select(AdmittedOperator).where(
            AdmittedOperator.tenant_id == self._tenant_id,
            AdmittedOperator.brand_json_url == brand_json_url,
        )
        return self._session.scalars(stmt).first()

    def create(
        self,
        *,
        operator_id: str,
        brand_json_url: str,
        display_name: str,
        aao_member_slug: str | None = None,
        house_domain: str | None = None,
        is_trusted: bool = False,
    ) -> AdmittedOperator:
        row = AdmittedOperator(
            tenant_id=self._tenant_id,
            operator_id=operator_id,
            brand_json_url=brand_json_url,
            aao_member_slug=aao_member_slug,
            house_domain=house_domain,
            display_name=display_name,
            is_trusted=is_trusted,
            is_active=True,
        )
        self._session.add(row)
        return row

    def deactivate(self, operator_id: str) -> bool:
        row = self.get_by_id(operator_id)
        if row is None:
            return False
        row.is_active = False
        return True

    def record_resolution(
        self,
        operator_id: str,
        *,
        success: bool,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Record a brand.json resolution attempt — observability only."""
        row = self.get_by_id(operator_id)
        if row is None:
            return
        ts = now or datetime.now(UTC)
        if success:
            row.last_resolved_at = ts
            row.last_resolution_error = None
        else:
            row.last_resolution_error = error_code
