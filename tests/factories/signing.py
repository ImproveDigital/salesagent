"""Factory_boy factories for signing-related models.

PR 1 of [signing-non-embedded](../../../docs/design/signing-non-embedded.md):
fixtures for the four new tables (admitted_operators, operator_advertiser_link,
tenant_signing_policy, tenant_signing_credentials).
"""

from __future__ import annotations

import factory
from factory import LazyAttribute, Sequence, SubFactory

from src.core.database.models import (
    AdmittedOperator,
    OperatorAdvertiserLink,
    TenantSigningCredential,
    TenantSigningPolicy,
)
from tests.factories.core import TenantFactory
from tests.factories.principal import PrincipalFactory


class AdmittedOperatorFactory(factory.alchemy.SQLAlchemyModelFactory):
    class Meta:
        model = AdmittedOperator
        sqlalchemy_session = None
        sqlalchemy_session_persistence = "commit"
        exclude = ("tenant",)

    tenant = SubFactory(TenantFactory)
    tenant_id = LazyAttribute(lambda o: o.tenant.tenant_id)
    operator_id = Sequence(lambda n: f"operator_{n:04d}")
    brand_json_url = LazyAttribute(lambda o: f"https://operator-{o.operator_id}.example.com/.well-known/brand.json")
    aao_member_slug = LazyAttribute(lambda o: f"aao-{o.operator_id}")
    house_domain = LazyAttribute(lambda o: f"operator-{o.operator_id}.example.com")
    display_name = LazyAttribute(lambda o: f"Test Operator {o.operator_id}")
    is_trusted = False
    is_active = True


class OperatorAdvertiserLinkFactory(factory.alchemy.SQLAlchemyModelFactory):
    class Meta:
        model = OperatorAdvertiserLink
        sqlalchemy_session = None
        sqlalchemy_session_persistence = "commit"
        exclude = ("operator", "principal")

    operator = SubFactory(AdmittedOperatorFactory)
    principal = SubFactory(
        PrincipalFactory,
        tenant=factory.SelfAttribute("..operator.tenant"),
    )
    tenant_id = LazyAttribute(lambda o: o.operator.tenant_id)
    operator_id = LazyAttribute(lambda o: o.operator.operator_id)
    principal_id = LazyAttribute(lambda o: o.principal.principal_id)
    billing_mode = "operator_bills"
    is_active = True


class TenantSigningPolicyFactory(factory.alchemy.SQLAlchemyModelFactory):
    class Meta:
        model = TenantSigningPolicy
        sqlalchemy_session = None
        sqlalchemy_session_persistence = "commit"
        exclude = ("tenant",)

    tenant = SubFactory(TenantFactory)
    tenant_id = LazyAttribute(lambda o: o.tenant.tenant_id)
    enabled = False
    required_for = factory.LazyFunction(list)
    covers_digest_policy = "either"
    max_skew_seconds = 60
    max_window_seconds = 300


class TenantSigningCredentialFactory(factory.alchemy.SQLAlchemyModelFactory):
    class Meta:
        model = TenantSigningCredential
        sqlalchemy_session = None
        sqlalchemy_session_persistence = "commit"
        exclude = ("tenant",)

    tenant = SubFactory(TenantFactory)
    tenant_id = LazyAttribute(lambda o: o.tenant.tenant_id)
    purpose = "webhook-signing"
    backend = "local_pem"
    backend_ref = LazyAttribute(lambda o: f"/tmp/signing-keys/{o.tenant_id}.pem")
    key_id = Sequence(lambda n: f"kid-{n:04d}")
    public_jwk = factory.LazyFunction(lambda: {"kty": "OKP", "crv": "Ed25519", "x": "test-public-key-bytes"})
    is_active = True
