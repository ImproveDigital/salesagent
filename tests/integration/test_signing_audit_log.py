"""PR 2D: verified_* columns on audit_logs are populated for signed requests.

The middleware sets verified state on the contextvar; ResolvedIdentity threads
it; log_tool_activity reads it off the identity and forwards to AuditLogger,
which writes the values to audit_logs.{verified_operator_id,
verified_agent_url, verified_key_id}.

This test bypasses the HTTP transport and exercises the audit-write half of
the chain by:
1. Manually setting the verified-state contextvar (as the middleware would)
2. Building a ResolvedIdentity with the verified fields populated
3. Calling log_tool_activity with that identity
4. Asserting the resulting AuditLog row has the verified_* columns set

Confirms the schema column → ORM model → AuditLogger.log_operation →
log_tool_activity wiring is end-to-end.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as SASession

from src.core.database.database_session import get_engine
from src.core.database.models import AuditLog
from src.core.helpers.activity_helpers import log_tool_activity
from src.core.resolved_identity import ResolvedIdentity
from src.core.signing import (
    VerifiedRequestState,
    clear_verified_state,
    set_verified_state,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


@pytest.fixture
def session(integration_db):
    from tests.factories import ALL_FACTORIES

    engine = get_engine()
    sess = SASession(bind=engine)
    try:
        for f in ALL_FACTORIES:
            f._meta.sqlalchemy_session = sess
        yield sess
    finally:
        sess.close()
        clear_verified_state()


class TestAuditLogVerifiedColumns:
    """audit_logs.verified_* are populated when a signed request runs."""

    def test_log_tool_activity_writes_verified_fields(self, session):
        from tests.factories import PrincipalFactory, TenantFactory

        tenant = TenantFactory(tenant_id="t_audit_verified")
        principal = PrincipalFactory(
            tenant=tenant,
            principal_id="p_audit_verified",
        )
        session.commit()

        # Simulate the middleware having verified a signature on this request.
        set_verified_state(
            VerifiedRequestState(
                operator_id="op_audited",
                agent_url="https://buyer.example.com/agents/buying",
                key_id="kid-audit-test",
            )
        )

        identity = ResolvedIdentity(
            principal_id=principal.principal_id,
            tenant_id=tenant.tenant_id,
            tenant={"tenant_id": tenant.tenant_id},
            verified_operator_id="op_audited",
            verified_agent_url="https://buyer.example.com/agents/buying",
            verified_key_id="kid-audit-test",
        )

        log_tool_activity(identity, "create_media_buy")

        # AuditLogger commits its own session; query a fresh one to read.
        engine = get_engine()
        with SASession(bind=engine) as fresh:
            stmt = select(AuditLog).filter_by(tenant_id=tenant.tenant_id).order_by(AuditLog.timestamp.desc()).limit(1)
            row = fresh.scalars(stmt).first()

        assert row is not None
        assert row.verified_operator_id == "op_audited"
        assert row.verified_agent_url == "https://buyer.example.com/agents/buying"
        assert row.verified_key_id == "kid-audit-test"

    def test_log_tool_activity_unsigned_leaves_verified_null(self, session):
        from tests.factories import PrincipalFactory, TenantFactory

        tenant = TenantFactory(tenant_id="t_audit_unsigned")
        principal = PrincipalFactory(
            tenant=tenant,
            principal_id="p_audit_unsigned",
        )
        session.commit()

        # No middleware verify happened on this request.
        clear_verified_state()

        identity = ResolvedIdentity(
            principal_id=principal.principal_id,
            tenant_id=tenant.tenant_id,
            tenant={"tenant_id": tenant.tenant_id},
        )

        log_tool_activity(identity, "get_products")

        engine = get_engine()
        with SASession(bind=engine) as fresh:
            stmt = select(AuditLog).filter_by(tenant_id=tenant.tenant_id).order_by(AuditLog.timestamp.desc()).limit(1)
            row = fresh.scalars(stmt).first()

        assert row is not None
        assert row.verified_operator_id is None
        assert row.verified_agent_url is None
        assert row.verified_key_id is None
