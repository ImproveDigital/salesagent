"""OperatorAdvertiserLink repository — (tenant, operator, advertiser) policy.

PR 1 of [signing-non-embedded](../../../../docs/design/signing-non-embedded.md).
Carries the per-link billing-mode bit. Authorization gate; the verifier middleware
checks the link's ``is_active`` before fetching any signing keys.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.database.models import OperatorAdvertiserLink

VALID_BILLING_MODES = ("operator_bills", "agent_billed", "disabled")


class OperatorAdvertiserLinkRepository:
    """Tenant-scoped CRUD against ``operator_advertiser_link``."""

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def list_for_operator(self, operator_id: str) -> list[OperatorAdvertiserLink]:
        stmt = (
            select(OperatorAdvertiserLink)
            .where(
                OperatorAdvertiserLink.tenant_id == self._tenant_id,
                OperatorAdvertiserLink.operator_id == operator_id,
            )
            .order_by(OperatorAdvertiserLink.created_at)
        )
        return list(self._session.scalars(stmt).all())

    def list_for_principal(self, principal_id: str) -> list[OperatorAdvertiserLink]:
        stmt = (
            select(OperatorAdvertiserLink)
            .where(
                OperatorAdvertiserLink.tenant_id == self._tenant_id,
                OperatorAdvertiserLink.principal_id == principal_id,
            )
            .order_by(OperatorAdvertiserLink.created_at)
        )
        return list(self._session.scalars(stmt).all())

    def get(self, operator_id: str, principal_id: str) -> OperatorAdvertiserLink | None:
        stmt = select(OperatorAdvertiserLink).where(
            OperatorAdvertiserLink.tenant_id == self._tenant_id,
            OperatorAdvertiserLink.operator_id == operator_id,
            OperatorAdvertiserLink.principal_id == principal_id,
        )
        return self._session.scalars(stmt).first()

    def upsert(
        self,
        *,
        operator_id: str,
        principal_id: str,
        billing_mode: str = "operator_bills",
        is_active: bool = True,
    ) -> OperatorAdvertiserLink:
        if billing_mode not in VALID_BILLING_MODES:
            raise ValueError(f"billing_mode must be one of {VALID_BILLING_MODES}; got {billing_mode!r}")
        existing = self.get(operator_id, principal_id)
        if existing is not None:
            existing.billing_mode = billing_mode
            existing.is_active = is_active
            return existing
        row = OperatorAdvertiserLink(
            tenant_id=self._tenant_id,
            operator_id=operator_id,
            principal_id=principal_id,
            billing_mode=billing_mode,
            is_active=is_active,
        )
        self._session.add(row)
        return row

    def deactivate(self, operator_id: str, principal_id: str) -> bool:
        row = self.get(operator_id, principal_id)
        if row is None:
            return False
        row.is_active = False
        return True
