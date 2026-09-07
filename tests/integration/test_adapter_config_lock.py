"""Integration tests for the adapter-configuration lock.

Lock state is stored in ``adapter_config.config_locked_at``: stamped by the
inventory-sync completion paths, backfilled by migration for already-synced
tenants. While set, the connection identity is frozen — ``Tenant.ad_server``,
``AdapterConfig.adapter_type``, ``gam_network_code``, and the
``client_id``/``client_secret`` keys inside ``config_json``. Everything else
(credentials like the GAM refresh token, templates, AXE keys, other config
fields) stays editable; connecting a different ad server or GAM network
requires creating a new tenant. Clearing the stamp requires
``super_admin_override``.

Covers the model-layer guard in ``src/core/database/adapter_config_lock.py``,
the sync-completion stamping, and the route-layer 403s on the admin write
endpoints.
"""

import os
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from src.core.database.adapter_config_lock import (
    AdapterConfigLockedError,
    is_adapter_config_locked,
)
from src.core.database.repositories.adapter_config import AdapterConfigRepository
from tests.factories import AdapterConfigFactory, SyncJobFactory, TenantFactory

pytestmark = pytest.mark.requires_db

_TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()


@pytest.fixture
def _encryption_key():
    with patch.dict(os.environ, {"ENCRYPTION_KEY": _TEST_ENCRYPTION_KEY}):
        yield


def _gam_tenant(tenant_id: str, locked: bool = True):
    tenant = TenantFactory(tenant_id=tenant_id, ad_server="google_ad_manager")
    adapter_config = AdapterConfigFactory(
        tenant=tenant,
        adapter_type="google_ad_manager",
        gam_network_code="111222333",
        gam_refresh_token="tok_original",
        config_locked_at=datetime.now(UTC) if locked else None,
    )
    return tenant, adapter_config


class TestLockStateAndStamping:
    """config_locked_at is the stored lock state, stamped on inventory-sync success."""

    def test_unlocked_by_default(self, factory_session):
        tenant = TenantFactory(tenant_id="lock_state_none")
        AdapterConfigFactory(tenant=tenant)
        assert is_adapter_config_locked(factory_session, tenant.tenant_id) is False

    def test_locked_when_stamp_set(self, factory_session):
        _gam_tenant("lock_state_set", locked=True)
        assert is_adapter_config_locked(factory_session, "lock_state_set") is True

    def test_sync_row_alone_does_not_lock(self, factory_session):
        """The stored stamp is the single source of truth — history rows don't lock."""
        tenant, _ = _gam_tenant("lock_state_rows", locked=False)
        SyncJobFactory(tenant=tenant, status="completed", sync_type="inventory")
        assert is_adapter_config_locked(factory_session, tenant.tenant_id) is False

    def test_missing_adapter_config_is_unlocked(self, factory_session):
        tenant = TenantFactory(tenant_id="lock_state_missing")
        assert is_adapter_config_locked(factory_session, tenant.tenant_id) is False

    def test_repository_stamp_is_idempotent(self, factory_session):
        tenant, adapter_config = _gam_tenant("lock_state_stamp", locked=False)
        repo = AdapterConfigRepository(factory_session, tenant.tenant_id)

        assert repo.mark_config_locked() is True
        factory_session.commit()
        first_stamp = adapter_config.config_locked_at
        assert first_stamp is not None

        assert repo.mark_config_locked() is False
        factory_session.commit()
        assert adapter_config.config_locked_at == first_stamp

    def test_inventory_sync_completion_stamps_lock(self, factory_session):
        """The GAM background-sync completion path locks the tenant."""
        from src.services.background_sync_service import _mark_sync_complete

        tenant, _ = _gam_tenant("lock_state_sync", locked=False)
        job = SyncJobFactory(tenant=tenant, status="running", sync_type="inventory")

        _mark_sync_complete(job.sync_id, {"ad_units": {"total": 1}})

        assert is_adapter_config_locked(factory_session, tenant.tenant_id) is True

    def test_non_inventory_sync_completion_does_not_lock(self, factory_session):
        from src.services.background_sync_service import _mark_sync_complete

        tenant, _ = _gam_tenant("lock_state_kind", locked=False)
        job = SyncJobFactory(tenant=tenant, status="running", sync_type="custom_targeting")

        _mark_sync_complete(job.sync_id, {})

        assert is_adapter_config_locked(factory_session, tenant.tenant_id) is False


class TestModelGuard:
    """The before_update listeners block config changes on locked tenants."""

    def test_adapter_type_change_blocked_when_locked(self, factory_session):
        _, adapter_config = _gam_tenant("lock_guard_type")

        adapter_config.adapter_type = "mock"
        with pytest.raises(AdapterConfigLockedError):
            factory_session.commit()
        factory_session.rollback()

    def test_gam_network_code_change_blocked_when_locked(self, factory_session):
        _, adapter_config = _gam_tenant("lock_guard_code")

        adapter_config.gam_network_code = "999888777"
        with pytest.raises(AdapterConfigLockedError):
            factory_session.commit()
        factory_session.rollback()

    def test_clearing_gam_network_code_blocked_when_locked(self, factory_session):
        _, adapter_config = _gam_tenant("lock_guard_clear")

        adapter_config.gam_network_code = None
        with pytest.raises(AdapterConfigLockedError):
            factory_session.commit()
        factory_session.rollback()

    def test_tenant_ad_server_change_blocked_when_locked(self, factory_session):
        tenant, _ = _gam_tenant("lock_guard_server")

        tenant.ad_server = "mock"
        with pytest.raises(AdapterConfigLockedError):
            factory_session.commit()
        factory_session.rollback()

    def test_changes_allowed_before_lock(self, factory_session):
        tenant, adapter_config = _gam_tenant("lock_guard_free", locked=False)

        adapter_config.adapter_type = "mock"
        adapter_config.gam_network_code = None
        tenant.ad_server = "mock"
        factory_session.commit()

        assert adapter_config.adapter_type == "mock"
        assert tenant.ad_server == "mock"

    def test_same_value_reassignment_allowed_when_locked(self, factory_session):
        """The settings forms resubmit the stored values on every save."""
        tenant, adapter_config = _gam_tenant("lock_guard_same")

        adapter_config.adapter_type = "google_ad_manager"
        adapter_config.gam_network_code = "111222333"
        tenant.ad_server = "google_ad_manager"
        factory_session.commit()

    def test_credentials_and_mirror_flags_writable_when_locked(self, factory_session):
        """Credential rotation and business-rule mirrors stay editable after lock."""
        _, adapter_config = _gam_tenant("lock_guard_writable")

        adapter_config.gam_refresh_token = "tok_rotated"
        adapter_config.gam_auth_method = "service_account"
        adapter_config.gam_manual_approval_required = True
        adapter_config.gam_network_currency = "EUR"
        factory_session.commit()

        assert adapter_config.gam_refresh_token == "tok_rotated"

    def test_other_settings_writable_when_locked(self, factory_session):
        """Only the connection identity is frozen — templates, AXE keys, and
        non-client config fields stay editable."""
        _, adapter_config = _gam_tenant("lock_guard_settings")

        adapter_config.gam_order_name_template = "Order {po_number}"
        adapter_config.axe_include_key = "hb_pb"
        adapter_config.gam_trafficker_id = "trafficker_2"
        adapter_config.config_json = {"username": "new_user"}
        factory_session.commit()

        assert adapter_config.gam_order_name_template == "Order {po_number}"
        assert adapter_config.config_json == {"username": "new_user"}

    def test_clearing_lock_stamp_blocked_without_override(self, factory_session):
        """config_locked_at is itself locked — unlocking requires super_admin_override."""
        _, adapter_config = _gam_tenant("lock_guard_unlock_deny")

        adapter_config.config_locked_at = None
        with pytest.raises(AdapterConfigLockedError):
            factory_session.commit()
        factory_session.rollback()

    def test_super_admin_override_unlocks(self, factory_session):
        """The documented unlock procedure: clear the stamp under super_admin_override."""
        _, adapter_config = _gam_tenant("lock_guard_unlock")

        factory_session.info["super_admin_override"] = True
        try:
            adapter_config.config_locked_at = None
            factory_session.commit()
        finally:
            factory_session.info.pop("super_admin_override", None)

        assert is_adapter_config_locked(factory_session, "lock_guard_unlock") is False

        # Fully unlocked: identity changes work again without any flag.
        adapter_config.adapter_type = "mock"
        factory_session.commit()
        assert adapter_config.adapter_type == "mock"

    def test_super_admin_override_bypasses_lock(self, factory_session):
        _, adapter_config = _gam_tenant("lock_guard_override")

        factory_session.info["super_admin_override"] = True
        try:
            adapter_config.gam_network_code = "444555666"
            factory_session.commit()
        finally:
            factory_session.info.pop("super_admin_override", None)

        assert adapter_config.gam_network_code == "444555666"


class TestAdminRoutes:
    """The admin write endpoints reject config changes with a clean 403."""

    def test_update_adapter_switch_returns_403_when_locked(self, authenticated_admin_client, factory_session):
        tenant, _ = _gam_tenant("lock_route_switch")

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/settings/adapter",
            json={"adapter": "mock"},
        )
        assert resp.status_code == 403
        assert "locked" in resp.get_json()["error"].lower()

    def test_update_adapter_edit_config_returns_403_when_locked(self, authenticated_admin_client, factory_session):
        tenant, _ = _gam_tenant("lock_route_edit")

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/settings/adapter",
            json={"adapter": "google_ad_manager", "action": "edit_config"},
        )
        assert resp.status_code == 403

    def test_update_adapter_network_code_change_returns_403_when_locked(
        self, authenticated_admin_client, factory_session
    ):
        tenant, _ = _gam_tenant("lock_route_code")

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/settings/adapter",
            json={"adapter": "google_ad_manager", "gam_network_code": "999888777"},
        )
        assert resp.status_code == 403

    def test_update_adapter_same_values_resubmit_allowed_when_locked(self, authenticated_admin_client, factory_session):
        """Forms resubmit stored values on save — a no-op write must pass."""
        tenant, _ = _gam_tenant("lock_route_ok")

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/settings/adapter",
            json={"adapter": "google_ad_manager", "gam_network_code": "111222333"},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_update_adapter_template_change_allowed_when_locked(self, authenticated_admin_client, factory_session):
        """Naming templates and AXE keys are not part of the connection identity."""
        tenant, _ = _gam_tenant("lock_route_template")

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/settings/adapter",
            json={
                "adapter": "google_ad_manager",
                "gam_network_code": "111222333",
                "order_name_template": "Order {po_number}",
                "axe_include_key": "hb_pb_new",
            },
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_update_adapter_switch_allowed_when_not_locked(self, authenticated_admin_client, factory_session):
        tenant, _ = _gam_tenant("lock_route_free", locked=False)

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/settings/adapter",
            json={"adapter": "mock"},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_save_adapter_config_switch_returns_403_when_locked(self, authenticated_admin_client, factory_session):
        tenant, _ = _gam_tenant("lock_route_cfg")

        resp = authenticated_admin_client.post(
            f"/api/tenant/{tenant.tenant_id}/adapter-config",
            json={"adapter_type": "mock", "config": {}},
        )
        assert resp.status_code == 403
        assert "locked" in resp.get_json()["error"].lower()

    def test_save_adapter_config_other_fields_allowed_when_locked(self, authenticated_admin_client, factory_session):
        """Config fields outside client_id/client_secret stay editable after lock."""
        tenant = TenantFactory(tenant_id="lock_route_mock", ad_server="mock")
        AdapterConfigFactory(tenant=tenant, adapter_type="mock")

        # Seed config_json through the route while unlocked, then lock.
        resp = authenticated_admin_client.post(
            f"/api/tenant/{tenant.tenant_id}/adapter-config",
            json={"adapter_type": "mock", "config": {"dry_run": False}},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        AdapterConfigRepository(factory_session, tenant.tenant_id).mark_config_locked()
        factory_session.commit()

        resp = authenticated_admin_client.post(
            f"/api/tenant/{tenant.tenant_id}/adapter-config",
            json={"adapter_type": "mock", "config": {"dry_run": True}},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_save_adapter_config_client_credentials_locked(
        self, authenticated_admin_client, factory_session, _encryption_key
    ):
        """client_id/client_secret identify the network seat — read-only after lock."""
        tenant = TenantFactory(tenant_id="lock_route_fw", ad_server="freewheel")
        AdapterConfigFactory(tenant=tenant, adapter_type="freewheel")

        resp = authenticated_admin_client.post(
            f"/api/tenant/{tenant.tenant_id}/adapter-config",
            json={"adapter_type": "freewheel", "config": {"client_id": "cid_original", "client_secret": "cs_original"}},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        AdapterConfigRepository(factory_session, tenant.tenant_id).mark_config_locked()
        factory_session.commit()

        # Changing the client_id is rejected.
        resp = authenticated_admin_client.post(
            f"/api/tenant/{tenant.tenant_id}/adapter-config",
            json={"adapter_type": "freewheel", "config": {"client_id": "cid_other", "client_secret": "cs_original"}},
        )
        assert resp.status_code == 403
        assert "locked" in resp.get_json()["error"].lower()

        # Changing the client_secret is rejected.
        resp = authenticated_admin_client.post(
            f"/api/tenant/{tenant.tenant_id}/adapter-config",
            json={"adapter_type": "freewheel", "config": {"client_id": "cid_original", "client_secret": "cs_other"}},
        )
        assert resp.status_code == 403

        # Editing other fields — client_secret omitted (preserved) — is allowed.
        resp = authenticated_admin_client.post(
            f"/api/tenant/{tenant.tenant_id}/adapter-config",
            json={
                "adapter_type": "freewheel",
                "config": {"client_id": "cid_original", "environment": "staging"},
            },
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_save_adapter_config_api_base_url_locked(
        self, authenticated_admin_client, factory_session, _encryption_key
    ):
        """The Improve Digital API base URL is part of the connection identity — read-only after lock."""
        tenant = TenantFactory(tenant_id="lock_route_impd", ad_server="improvedigital")
        AdapterConfigFactory(tenant=tenant, adapter_type="improvedigital")

        resp = authenticated_admin_client.post(
            f"/api/tenant/{tenant.tenant_id}/adapter-config",
            json={
                "adapter_type": "improvedigital",
                "config": {
                    "client_id": "cid_impd",
                    "client_secret": "cs_impd",
                    "api_base_url": "https://api.360yield.com",
                },
            },
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        AdapterConfigRepository(factory_session, tenant.tenant_id).mark_config_locked()
        factory_session.commit()

        # Changing the API base URL (e.g. production -> dev host) is rejected.
        resp = authenticated_admin_client.post(
            f"/api/tenant/{tenant.tenant_id}/adapter-config",
            json={
                "adapter_type": "improvedigital",
                "config": {
                    "client_id": "cid_impd",
                    "api_base_url": "https://api.360yielddev.com",
                },
            },
        )
        assert resp.status_code == 403
        assert "locked" in resp.get_json()["error"].lower()

        # Editing another field with the same base URL (secret omitted/preserved) is allowed.
        resp = authenticated_admin_client.post(
            f"/api/tenant/{tenant.tenant_id}/adapter-config",
            json={
                "adapter_type": "improvedigital",
                "config": {
                    "client_id": "cid_impd",
                    "api_base_url": "https://api.360yield.com",
                    "buying_entity_id": 421,
                },
            },
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_settings_page_renders_locked_client_credentials(self, authenticated_admin_client, factory_session):
        """The Improve Digital client_id/client_secret inputs render read-only when locked."""
        tenant = TenantFactory(tenant_id="lock_ui_impd", ad_server="improvedigital")
        AdapterConfigFactory(
            tenant=tenant,
            adapter_type="improvedigital",
            config_json={"client_id": "cid_locked"},
            config_locked_at=datetime.now(UTC),
        )

        resp = authenticated_admin_client.get(f"/tenant/{tenant.tenant_id}/settings/adapter")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "Ad Server Connection Locked" in html
        client_id_input = html.split('id="improvedigital_client_id"')[1].split(">")[0]
        assert "readonly" in client_id_input
        client_secret_input = html.split('id="improvedigital_client_secret"')[1].split(">")[0]
        assert "readonly" in client_secret_input
        api_base_url_input = html.split('id="improvedigital_api_base_url"')[1].split(">")[0]
        assert "readonly" in api_base_url_input

    def test_settings_page_renders_locked_gam_network_code(self, authenticated_admin_client, factory_session):
        """The GAM wizard's editable network-code input renders read-only when
        locked (the input only appears when a refresh token exists but no
        network code is stored — fully-configured tenants get display-only text)."""
        tenant = TenantFactory(tenant_id="lock_ui_gam", ad_server="google_ad_manager")
        AdapterConfigFactory(
            tenant=tenant,
            adapter_type="google_ad_manager",
            gam_network_code=None,
            gam_refresh_token="tok_x",
            config_locked_at=datetime.now(UTC),
        )

        resp = authenticated_admin_client.get(f"/tenant/{tenant.tenant_id}/settings/adapter")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        network_code_input = html.split('id="gam_network_code"')[1].split(">")[0]
        assert "readonly" in network_code_input

    def test_configure_gam_network_change_returns_403_when_locked(self, authenticated_admin_client, factory_session):
        tenant, _ = _gam_tenant("lock_route_gam")

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/gam/configure",
            json={"auth_method": "oauth", "network_code": "999888777", "refresh_token": "tok_new"},
        )
        assert resp.status_code == 403

    def test_configure_gam_token_rotation_allowed_when_locked(self, authenticated_admin_client, factory_session):
        tenant, _ = _gam_tenant("lock_route_gam_rotate")

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/gam/configure",
            json={"auth_method": "oauth", "network_code": "111222333", "refresh_token": "tok_new"},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
