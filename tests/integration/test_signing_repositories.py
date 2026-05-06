"""Integration tests for the four signing repositories.

PR 1 of [signing-non-embedded](../../../docs/design/signing-non-embedded.md):
verifies CRUD + constraints against a real PostgreSQL database. Constraint
checks are the high-value cases — they live in the migration, and the only way
to confirm they hold is round-tripping through psycopg.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SASession

from src.core.database.database_session import get_engine
from src.core.database.repositories import (
    AdmittedOperatorRepository,
    OperatorAdvertiserLinkRepository,
    TenantSigningCredentialRepository,
    TenantSigningPolicyRepository,
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


class TestAdmittedOperatorRepository:
    def test_create_lists_active_only(self, session):
        from tests.factories import AdmittedOperatorFactory, TenantFactory

        tenant = TenantFactory(tenant_id="t_aor_active")
        active = AdmittedOperatorFactory(tenant=tenant, operator_id="op_active")
        inactive = AdmittedOperatorFactory(tenant=tenant, operator_id="op_inactive", is_active=False)
        session.commit()

        repo = AdmittedOperatorRepository(session, tenant.tenant_id)
        active_ids = [r.operator_id for r in repo.list_active()]
        all_ids = [r.operator_id for r in repo.list_all()]

        assert active.operator_id in active_ids
        assert inactive.operator_id not in active_ids
        assert {active.operator_id, inactive.operator_id}.issubset(all_ids)

    def test_unique_brand_json_url_per_tenant(self, session):
        from tests.factories import AdmittedOperatorFactory, TenantFactory

        tenant = TenantFactory(tenant_id="t_aor_unique")
        AdmittedOperatorFactory(
            tenant=tenant,
            operator_id="op_a",
            brand_json_url="https://op.example.com/.well-known/brand.json",
        )

        with pytest.raises(IntegrityError):
            AdmittedOperatorFactory(
                tenant=tenant,
                operator_id="op_b",
                brand_json_url="https://op.example.com/.well-known/brand.json",
            )
        session.rollback()


class TestOperatorAdvertiserLinkRepository:
    def test_billing_mode_check_constraint(self, session):
        from tests.factories import (
            AdmittedOperatorFactory,
            PrincipalFactory,
            TenantFactory,
        )

        tenant = TenantFactory(tenant_id="t_link_billing")
        operator = AdmittedOperatorFactory(tenant=tenant, operator_id="op_link")
        principal = PrincipalFactory(tenant=tenant, principal_id="p_link")
        session.commit()

        repo = OperatorAdvertiserLinkRepository(session, tenant.tenant_id)
        with pytest.raises(ValueError):
            repo.upsert(
                operator_id=operator.operator_id,
                principal_id=principal.principal_id,
                billing_mode="bogus_mode",
            )

    def test_upsert_round_trip(self, session):
        from tests.factories import (
            AdmittedOperatorFactory,
            PrincipalFactory,
            TenantFactory,
        )

        tenant = TenantFactory(tenant_id="t_link_upsert")
        operator = AdmittedOperatorFactory(tenant=tenant, operator_id="op_u")
        principal = PrincipalFactory(tenant=tenant, principal_id="p_u")
        session.commit()

        repo = OperatorAdvertiserLinkRepository(session, tenant.tenant_id)
        first = repo.upsert(
            operator_id=operator.operator_id,
            principal_id=principal.principal_id,
            billing_mode="agent_billed",
        )
        session.commit()
        assert first.billing_mode == "agent_billed"

        updated = repo.upsert(
            operator_id=operator.operator_id,
            principal_id=principal.principal_id,
            billing_mode="disabled",
        )
        session.commit()
        assert updated.billing_mode == "disabled"
        assert updated.is_active is True


class TestTenantSigningPolicyRepository:
    def test_default_policy_when_missing(self, session):
        from tests.factories import TenantFactory

        tenant = TenantFactory(tenant_id="t_policy_default")
        session.commit()

        repo = TenantSigningPolicyRepository(session, tenant.tenant_id)
        default = repo.get_or_default()

        assert default.enabled is False
        assert list(default.required_for) == []
        assert default.covers_digest_policy == "either"
        assert default.max_skew_seconds == 60
        assert default.max_window_seconds == 300

    def test_upsert_persists_required_for(self, session):
        from tests.factories import TenantFactory

        tenant = TenantFactory(tenant_id="t_policy_upsert")
        session.commit()

        repo = TenantSigningPolicyRepository(session, tenant.tenant_id)
        repo.upsert(enabled=True, required_for=["create_media_buy", "update_media_buy"])
        session.commit()

        roundtrip = repo.get()
        assert roundtrip is not None
        assert roundtrip.enabled is True
        assert sorted(roundtrip.required_for) == ["create_media_buy", "update_media_buy"]

    def test_invalid_digest_policy_rejected(self, session):
        from tests.factories import TenantFactory

        tenant = TenantFactory(tenant_id="t_policy_invalid")
        session.commit()

        repo = TenantSigningPolicyRepository(session, tenant.tenant_id)
        with pytest.raises(ValueError):
            repo.upsert(covers_digest_policy="bogus")


class TestTenantSigningCredentialRepository:
    def test_invalid_backend_rejected(self, session):
        from tests.factories import TenantFactory

        tenant = TenantFactory(tenant_id="t_cred_backend")
        session.commit()

        repo = TenantSigningCredentialRepository(session, tenant.tenant_id)
        with pytest.raises(ValueError):
            repo.create(
                purpose="webhook-signing",
                backend="bogus",
                backend_ref="x",
                public_jwk={"kty": "OKP"},
                key_id="kid-x",
            )

    def test_rotate_out_marks_inactive(self, session):
        from tests.factories import TenantFactory, TenantSigningCredentialFactory

        tenant = TenantFactory(tenant_id="t_cred_rotate")
        cred = TenantSigningCredentialFactory(tenant=tenant)
        session.commit()

        repo = TenantSigningCredentialRepository(session, tenant.tenant_id)
        ok = repo.rotate_out(cred.purpose, cred.key_id)
        session.commit()

        assert ok is True
        rolled = repo.get_by_kid(cred.purpose, cred.key_id)
        assert rolled is not None
        assert rolled.is_active is False
        assert rolled.rotated_out_at is not None

    def test_get_active_returns_latest_not_rotated(self, session):
        from tests.factories import TenantFactory, TenantSigningCredentialFactory

        tenant = TenantFactory(tenant_id="t_cred_active")
        TenantSigningCredentialFactory(
            tenant=tenant,
            purpose="webhook-signing",
            key_id="kid-old",
            is_active=False,
        )
        new_cred = TenantSigningCredentialFactory(
            tenant=tenant,
            purpose="webhook-signing",
            key_id="kid-new",
            is_active=True,
        )
        session.commit()

        repo = TenantSigningCredentialRepository(session, tenant.tenant_id)
        active = repo.get_active("webhook-signing")
        assert active is not None
        assert active.key_id == new_cred.key_id


class TestPrincipalBoundOperator:
    def test_bound_operator_persists(self, session):
        from sqlalchemy import select

        from src.core.database.models import Principal
        from tests.factories import (
            AdmittedOperatorFactory,
            PrincipalFactory,
            TenantFactory,
        )

        tenant = TenantFactory(tenant_id="t_bound_op")
        operator = AdmittedOperatorFactory(tenant=tenant, operator_id="op_bp")
        principal = PrincipalFactory(tenant=tenant, principal_id="p_bp", bound_operator_id=operator.operator_id)
        session.commit()
        session.expire(principal)

        roundtrip = session.scalars(
            select(Principal).filter_by(tenant_id=tenant.tenant_id, principal_id="p_bp")
        ).first()
        assert roundtrip is not None
        assert roundtrip.bound_operator_id == operator.operator_id
