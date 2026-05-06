"""TenantSigningPolicy repository — per-tenant signing policy.

PR 1 of [signing-non-embedded](../../../../docs/design/signing-non-embedded.md).
One row per tenant. Master switch + per-operation requirement list + RFC 9421 knobs.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.database.models import TenantSigningPolicy

VALID_DIGEST_POLICIES = ("required", "forbidden", "either")


class TenantSigningPolicyRepository:
    """Tenant-scoped CRUD against ``tenant_signing_policy``."""

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def get(self) -> TenantSigningPolicy | None:
        stmt = select(TenantSigningPolicy).where(TenantSigningPolicy.tenant_id == self._tenant_id)
        return self._session.scalars(stmt).first()

    def get_or_default(self) -> TenantSigningPolicy:
        """Return existing policy row, or a transient default with sane defaults.

        The transient row is NOT added to the session — callers wanting to
        persist defaults should call :meth:`upsert` explicitly.
        """
        existing = self.get()
        if existing is not None:
            return existing
        return TenantSigningPolicy(
            tenant_id=self._tenant_id,
            enabled=False,
            required_for=[],
            covers_digest_policy="either",
            max_skew_seconds=60,
            max_window_seconds=300,
        )

    def upsert(
        self,
        *,
        enabled: bool | None = None,
        required_for: Iterable[str] | None = None,
        covers_digest_policy: str | None = None,
        max_skew_seconds: int | None = None,
        max_window_seconds: int | None = None,
    ) -> TenantSigningPolicy:
        if covers_digest_policy is not None and covers_digest_policy not in VALID_DIGEST_POLICIES:
            raise ValueError(
                f"covers_digest_policy must be one of {VALID_DIGEST_POLICIES}; got {covers_digest_policy!r}"
            )
        existing = self.get()
        if existing is None:
            row = TenantSigningPolicy(
                tenant_id=self._tenant_id,
                enabled=enabled if enabled is not None else False,
                required_for=list(required_for) if required_for is not None else [],
                covers_digest_policy=covers_digest_policy or "either",
                max_skew_seconds=max_skew_seconds if max_skew_seconds is not None else 60,
                max_window_seconds=max_window_seconds if max_window_seconds is not None else 300,
            )
            self._session.add(row)
            return row
        if enabled is not None:
            existing.enabled = enabled
        if required_for is not None:
            existing.required_for = list(required_for)
        if covers_digest_policy is not None:
            existing.covers_digest_policy = covers_digest_policy
        if max_skew_seconds is not None:
            existing.max_skew_seconds = max_skew_seconds
        if max_window_seconds is not None:
            existing.max_window_seconds = max_window_seconds
        return existing
