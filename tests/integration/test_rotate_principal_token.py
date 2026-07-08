"""Rotate-token admin action: issue a new bearer token for a Principal
without deleting/recreating it.

Before this route existed, an operator who needed to reissue a buyer's
credential had to delete the Principal (losing audit history and media buy
associations) and recreate it. Rotation invalidates the old token
immediately — there is no grace period.
"""

from __future__ import annotations

import pytest
from sqlalchemy import desc, select

from src.core.auth_utils import get_principal_from_token
from src.core.database.database_session import get_db_session
from src.core.database.models import AuditLog, Principal

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.admin]

_SAME_ORIGIN_HEADERS = {"Origin": "http://localhost"}


def _seed_principal(tenant_id: str, principal_id: str, *, access_token: str | None) -> None:
    from src.core.database.models import Tenant
    from tests.factories import ALL_FACTORIES, PrincipalFactory

    with get_db_session() as session:
        try:
            for f in ALL_FACTORIES:
                f._meta.sqlalchemy_session = session
            tenant = session.scalars(select(Tenant).filter_by(tenant_id=tenant_id)).first()
            assert tenant is not None, f"test fixture didn't create tenant {tenant_id!r}"
            PrincipalFactory(
                tenant=tenant,
                principal_id=principal_id,
                access_token=access_token,
            )
        finally:
            for f in ALL_FACTORIES:
                f._meta.sqlalchemy_session = None


def _latest_rotate_audit(tenant_id: str) -> AuditLog | None:
    with get_db_session() as session:
        return session.scalars(
            select(AuditLog)
            .filter_by(tenant_id=tenant_id, operation="AdminUI.rotate_principal_token")
            .order_by(desc(AuditLog.timestamp))
            .limit(1)
        ).first()


class TestRotatePrincipalToken:
    def test_rotate_token_issues_new_value_and_invalidates_old(
        self, authenticated_admin_session, test_tenant_with_data
    ):
        tenant_id = test_tenant_with_data["tenant_id"]
        principal_id = "p_rotate_basic"
        old_token = "old-service-token"
        _seed_principal(tenant_id, principal_id, access_token=old_token)

        response = authenticated_admin_session.post(
            f"/tenant/{tenant_id}/principals/{principal_id}/rotate-token",
            headers=_SAME_ORIGIN_HEADERS,
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        new_token = data["access_token"]
        assert new_token != old_token

        old_principal_id, _ = get_principal_from_token(old_token, tenant_id=tenant_id)
        assert old_principal_id is None, "old token must stop working immediately"

        new_principal_id, _ = get_principal_from_token(new_token, tenant_id=tenant_id)
        assert new_principal_id == principal_id

    def test_rotate_token_stamps_rotated_at(self, authenticated_admin_session, test_tenant_with_data):
        tenant_id = test_tenant_with_data["tenant_id"]
        principal_id = "p_rotate_stamps"
        _seed_principal(tenant_id, principal_id, access_token="pre-rotate-token")

        authenticated_admin_session.post(
            f"/tenant/{tenant_id}/principals/{principal_id}/rotate-token",
            headers=_SAME_ORIGIN_HEADERS,
        )

        with get_db_session() as session:
            principal = session.scalars(
                select(Principal).filter_by(tenant_id=tenant_id, principal_id=principal_id)
            ).first()
            assert principal is not None
            assert principal.access_token_rotated_at is not None
            assert principal.access_token_created_at is not None

    def test_rotate_token_404_for_missing_principal(self, authenticated_admin_session, test_tenant_with_data):
        tenant_id = test_tenant_with_data["tenant_id"]

        response = authenticated_admin_session.post(
            f"/tenant/{tenant_id}/principals/does-not-exist/rotate-token",
            headers=_SAME_ORIGIN_HEADERS,
        )

        assert response.status_code == 404

    def test_rotate_token_400_when_principal_has_no_token(self, authenticated_admin_session, test_tenant_with_data):
        """Embedded-mode principals may legitimately have no bearer token."""
        tenant_id = test_tenant_with_data["tenant_id"]
        principal_id = "p_rotate_no_token"
        _seed_principal(tenant_id, principal_id, access_token=None)

        response = authenticated_admin_session.post(
            f"/tenant/{tenant_id}/principals/{principal_id}/rotate-token",
            headers=_SAME_ORIGIN_HEADERS,
        )

        assert response.status_code == 400

    def test_rotate_token_writes_audit_log_entry(self, authenticated_admin_session, test_tenant_with_data):
        tenant_id = test_tenant_with_data["tenant_id"]
        principal_id = "p_rotate_audit"
        _seed_principal(tenant_id, principal_id, access_token="pre-audit-token")

        authenticated_admin_session.post(
            f"/tenant/{tenant_id}/principals/{principal_id}/rotate-token",
            headers=_SAME_ORIGIN_HEADERS,
        )

        audit = _latest_rotate_audit(tenant_id)
        assert audit is not None, "rotate_principal_token audit row was not written"
        assert audit.details is not None
        assert audit.details.get("principal_id") == principal_id
