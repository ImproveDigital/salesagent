"""Manual publisher authorization on the Publishers page.

An admin can mark a publisher partner as authorized without our agent URL
being listed in the publisher's adagents.json. The partner then counts as
verified (setup checklist, product property selection) and keeps that state
across Refresh / Refresh-all until the publisher authorizes us for real or
the admin removes the manual authorization.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.admin.app import create_app
from src.core.config import AppConfig
from src.core.database.repositories.tenant_config import TenantConfigRepository
from src.services.aao_lookup_service import PublisherPartnerStatus
from src.services.setup_checklist_service import SetupChecklistService
from tests.factories import (
    AdapterConfigFactory,
    AuthorizedPropertyFactory,
    PublisherPartnerFactory,
    TenantFactory,
)
from tests.harness._base import IntegrationEnv

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

TENANT_ID = "manual_auth_tenant"
DOMAIN = "games-site.com"
OTHER_DOMAIN = "other-publisher.com"


def _status(kind: str, authorized: int = 0) -> PublisherPartnerStatus:
    return PublisherPartnerStatus(
        publisher_domain=DOMAIN,
        total_properties=3,
        authorized_properties=authorized,
        status=kind,
        aao_onboarding_url=f"https://agenticadvertising.org/publisher/{DOMAIN}",
        error=None,
    )


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SESSION_COOKIE_PATH"] = "/"
    with app.test_client() as test_client:
        yield test_client


def _login(client, role: str = "admin") -> None:
    with client.session_transaction() as sess:
        sess["test_user"] = "admin@example.com"
        sess["test_user_role"] = role
        sess["test_user_name"] = "Test Admin"
        sess["test_tenant_id"] = TENANT_ID


@pytest.fixture
def env(integration_db):
    """Non-mock tenant (real AAO path) with one unverified publisher partner."""
    with IntegrationEnv(tenant_id=TENANT_ID) as test_env:
        tenant = TenantFactory(tenant_id=TENANT_ID, ad_server="google_ad_manager", auth_setup_mode=True)
        AdapterConfigFactory(tenant=tenant, adapter_type="google_ad_manager")
        partner = PublisherPartnerFactory(
            tenant=tenant, publisher_domain=DOMAIN, is_verified=False, sync_status="pending"
        )
        test_env.partner_id = partner.id
        yield test_env


@pytest.fixture
def other_property(env):
    """Another publisher's property — must never be touched by actions on DOMAIN."""
    AuthorizedPropertyFactory(tenant=_tenant(env), property_id="other_site", publisher_domain=OTHER_DOMAIN)


@pytest.fixture
def aao(env):
    """Run the real (non-dev, non-mock) AAO branch with a stubbed publisher lookup."""
    with (
        patch("src.admin.blueprints.publisher_partners.get_config", return_value=AppConfig(environment="staging")),
        patch("src.admin.blueprints.publisher_partners.check_url_ssrf", return_value=(True, "")),
        patch("src.admin.blueprints.publisher_partners.get_publisher_partner_status", new_callable=AsyncMock) as lookup,
    ):
        yield lookup


def _partner(env):
    env._session.expire_all()
    return TenantConfigRepository(env._session, TENANT_ID).get_publisher_partner_by_domain(DOMAIN)


def _tenant(env):
    return TenantConfigRepository(env._session, TENANT_ID).get_tenant()


def _property_domains(env) -> list[str]:
    env._session.expire_all()
    return sorted(
        p.publisher_domain for p in TenantConfigRepository(env._session, TENANT_ID).list_authorized_properties()
    )


def _url(env) -> str:
    return f"/tenant/{TENANT_ID}/publisher-partners/{env.partner_id}/manual-authorization"


class TestAuthorizeManually:
    def test_marks_partner_verified_and_manual(self, client, env):
        _login(client)

        response = client.post(_url(env))

        assert response.status_code == 200
        body = response.get_json()
        assert body["is_verified"] is True
        assert body["aao_status"] == "manual"
        assert body["manual_authorization"] is True
        partner = _partner(env)
        assert partner.is_verified is True
        assert partner.aao_status_kind == "manual"

    def test_creates_all_inventory_property_for_the_domain(self, client, env):
        _login(client)

        client.post(_url(env))

        env._session.expire_all()
        repo = TenantConfigRepository(env._session, TENANT_ID)
        domain_properties = [p for p in repo.list_authorized_properties() if p.publisher_domain == DOMAIN]
        assert len(domain_properties) == 1
        assert domain_properties[0].tags == ["all_inventory"]
        assert "all_inventory" in {tag.tag_id for tag in repo.list_property_tags()}

    def test_completes_authorized_properties_checklist_item(self, client, env):
        def item_complete() -> bool:
            critical = SetupChecklistService(TENANT_ID).get_setup_status()["critical"]
            return next(t for t in critical if t["key"] == "authorized_properties")["is_complete"]

        assert item_complete() is False
        _login(client)

        client.post(_url(env))

        assert item_complete() is True

    def test_member_role_is_rejected(self, client, env):
        _login(client, role="member")

        response = client.post(_url(env))

        assert response.status_code == 403
        assert _partner(env).is_verified is False

    def test_unknown_partner_returns_404(self, client, env):
        _login(client)

        response = client.post(f"/tenant/{TENANT_ID}/publisher-partners/999999/manual-authorization")

        assert response.status_code == 404


class TestManualAuthorizationSurvivesRechecks:
    def test_refresh_all_keeps_manual_when_publisher_has_not_listed_us(self, client, env, aao):
        _login(client)
        client.post(_url(env))
        aao.return_value = _status("pending")

        response = client.post(f"/tenant/{TENANT_ID}/publisher-partners/sync")

        assert response.status_code == 200
        assert response.get_json()["verified"] == 1
        partner = _partner(env)
        assert partner.is_verified is True
        assert partner.aao_status_kind == "manual"

    def test_refresh_keeps_manual_when_publisher_has_not_listed_us(self, client, env, aao):
        _login(client)
        client.post(_url(env))
        aao.return_value = _status("unreachable")

        response = client.post(f"/tenant/{TENANT_ID}/publisher-partners/{env.partner_id}/refresh")

        assert response.status_code == 200
        assert response.get_json()["aao_status"] == "manual"
        assert _partner(env).is_verified is True

    def test_refresh_upgrades_to_real_authorization_once_publisher_lists_us(self, client, env, aao):
        _login(client)
        client.post(_url(env))
        aao.return_value = _status("authorized", authorized=3)

        response = client.post(f"/tenant/{TENANT_ID}/publisher-partners/{env.partner_id}/refresh")

        assert response.get_json()["aao_status"] == "authorized"
        partner = _partner(env)
        assert partner.aao_status_kind == "authorized"
        assert partner.is_verified is True

    def test_agent_url_change_keeps_manual_authorization(self, client, env):
        _login(client)
        client.post(_url(env))

        TenantConfigRepository(env._session, TENANT_ID).invalidate_publisher_partner_aao_statuses("agent URL changed")
        env._session.commit()

        partner = _partner(env)
        assert partner.is_verified is True
        assert partner.aao_status_kind == "manual"


class TestRemoveManualAuthorization:
    def test_returns_partner_to_pending(self, client, env):
        _login(client)
        client.post(_url(env))

        response = client.delete(_url(env))

        assert response.status_code == 200
        partner = _partner(env)
        assert partner.is_verified is False
        assert partner.aao_status_kind is None
        assert partner.sync_status == "pending"

    def test_removes_every_property_of_that_publisher_only(self, client, env, other_property):
        # e.g. the placeholder "Refresh all" adds in development mode
        AuthorizedPropertyFactory(tenant=_tenant(env), property_id="website_games_site_com", publisher_domain=DOMAIN)
        _login(client)
        client.post(_url(env))

        client.delete(_url(env))

        assert _property_domains(env) == [OTHER_DOMAIN]


class TestDeletePublisher:
    def test_removes_every_property_of_that_publisher_only(self, client, env, other_property):
        AuthorizedPropertyFactory(tenant=_tenant(env), property_id="games_site_com", publisher_domain=DOMAIN)

        _login(client)
        response = client.delete(f"/tenant/{TENANT_ID}/publisher-partners/{env.partner_id}")

        assert response.status_code == 200
        assert _property_domains(env) == [OTHER_DOMAIN]

    def test_removes_manual_property_of_manually_authorized_publisher(self, client, env, other_property):
        _login(client)
        client.post(_url(env))

        client.delete(f"/tenant/{TENANT_ID}/publisher-partners/{env.partner_id}")

        assert _property_domains(env) == [OTHER_DOMAIN]

    def test_partner_that_is_not_manual_is_rejected(self, client, env):
        _login(client)

        response = client.delete(_url(env))

        assert response.status_code == 409
