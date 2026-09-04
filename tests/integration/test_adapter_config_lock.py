"""Integration tests for the adapter-configuration lock.

Once a tenant has successfully synced inventory, the ad server identity
(``Tenant.ad_server``, ``AdapterConfig.adapter_type``,
``AdapterConfig.gam_network_code``) is frozen — connecting a different ad
server or GAM network requires creating a new tenant.

Covers the model-layer guard in ``src/core/database/adapter_config_lock.py``
and the route-layer 403s on the admin write endpoints.
"""

import pytest

from src.core.database.adapter_config_lock import (
    AdapterConfigLockedError,
    is_adapter_config_locked,
)
from tests.factories import AdapterConfigFactory, SyncJobFactory, TenantFactory
from tests.factories.core import GAMInventoryFactory

pytestmark = pytest.mark.requires_db


def _gam_tenant(tenant_id: str):
    tenant = TenantFactory(tenant_id=tenant_id, ad_server="google_ad_manager")
    adapter_config = AdapterConfigFactory(
        tenant=tenant,
        adapter_type="google_ad_manager",
        gam_network_code="111222333",
        gam_refresh_token="tok_original",
    )
    return tenant, adapter_config


class TestLockPredicate:
    """is_adapter_config_locked reflects the tenant's sync history."""

    def test_unlocked_without_any_sync(self, factory_session):
        tenant = TenantFactory(tenant_id="lock_pred_none")
        assert is_adapter_config_locked(factory_session, tenant.tenant_id) is False

    def test_locked_by_completed_inventory_sync(self, factory_session):
        tenant = TenantFactory(tenant_id="lock_pred_done")
        SyncJobFactory(tenant=tenant, status="completed", sync_type="inventory")
        assert is_adapter_config_locked(factory_session, tenant.tenant_id) is True

    def test_failed_sync_does_not_lock(self, factory_session):
        tenant = TenantFactory(tenant_id="lock_pred_fail")
        SyncJobFactory(tenant=tenant, status="failed", sync_type="inventory")
        assert is_adapter_config_locked(factory_session, tenant.tenant_id) is False

    def test_non_inventory_sync_does_not_lock(self, factory_session):
        tenant = TenantFactory(tenant_id="lock_pred_kind")
        SyncJobFactory(tenant=tenant, status="completed", sync_type="custom_targeting")
        assert is_adapter_config_locked(factory_session, tenant.tenant_id) is False

    def test_locked_by_existing_inventory_rows(self, factory_session):
        """Tenants that synced before sync_jobs recorded history are still locked."""
        tenant = TenantFactory(tenant_id="lock_pred_rows")
        GAMInventoryFactory(tenant=tenant)
        assert is_adapter_config_locked(factory_session, tenant.tenant_id) is True

    def test_other_tenants_sync_does_not_lock(self, factory_session):
        synced = TenantFactory(tenant_id="lock_pred_other_a")
        SyncJobFactory(tenant=synced, status="completed", sync_type="inventory")
        untouched = TenantFactory(tenant_id="lock_pred_other_b")
        assert is_adapter_config_locked(factory_session, untouched.tenant_id) is False


class TestModelGuard:
    """The before_update listeners block identity changes on synced tenants."""

    def test_adapter_type_change_blocked_when_locked(self, factory_session):
        tenant, adapter_config = _gam_tenant("lock_guard_type")
        SyncJobFactory(tenant=tenant)

        adapter_config.adapter_type = "mock"
        with pytest.raises(AdapterConfigLockedError):
            factory_session.commit()
        factory_session.rollback()

    def test_gam_network_code_change_blocked_when_locked(self, factory_session):
        tenant, adapter_config = _gam_tenant("lock_guard_code")
        SyncJobFactory(tenant=tenant)

        adapter_config.gam_network_code = "999888777"
        with pytest.raises(AdapterConfigLockedError):
            factory_session.commit()
        factory_session.rollback()

    def test_clearing_gam_network_code_blocked_when_locked(self, factory_session):
        tenant, adapter_config = _gam_tenant("lock_guard_clear")
        SyncJobFactory(tenant=tenant)

        adapter_config.gam_network_code = None
        with pytest.raises(AdapterConfigLockedError):
            factory_session.commit()
        factory_session.rollback()

    def test_tenant_ad_server_change_blocked_when_locked(self, factory_session):
        tenant, _ = _gam_tenant("lock_guard_server")
        SyncJobFactory(tenant=tenant)

        tenant.ad_server = "mock"
        with pytest.raises(AdapterConfigLockedError):
            factory_session.commit()
        factory_session.rollback()

    def test_changes_allowed_before_first_sync(self, factory_session):
        tenant, adapter_config = _gam_tenant("lock_guard_free")

        adapter_config.adapter_type = "mock"
        adapter_config.gam_network_code = None
        tenant.ad_server = "mock"
        factory_session.commit()

        assert adapter_config.adapter_type == "mock"
        assert tenant.ad_server == "mock"

    def test_same_value_reassignment_allowed_when_locked(self, factory_session):
        """The settings forms resubmit the stored values on every save."""
        tenant, adapter_config = _gam_tenant("lock_guard_same")
        SyncJobFactory(tenant=tenant)

        adapter_config.adapter_type = "google_ad_manager"
        adapter_config.gam_network_code = "111222333"
        tenant.ad_server = "google_ad_manager"
        factory_session.commit()

    def test_non_identity_fields_writable_when_locked(self, factory_session):
        """Credential rotation and naming templates stay editable after sync."""
        tenant, adapter_config = _gam_tenant("lock_guard_writable")
        SyncJobFactory(tenant=tenant)

        adapter_config.gam_refresh_token = "tok_rotated"
        adapter_config.gam_order_name_template = "Order {po_number}"
        adapter_config.gam_manual_approval_required = True
        factory_session.commit()

        assert adapter_config.gam_refresh_token == "tok_rotated"

    def test_super_admin_override_bypasses_lock(self, factory_session):
        tenant, adapter_config = _gam_tenant("lock_guard_override")
        SyncJobFactory(tenant=tenant)

        factory_session.info["super_admin_override"] = True
        try:
            adapter_config.gam_network_code = "444555666"
            factory_session.commit()
        finally:
            factory_session.info.pop("super_admin_override", None)

        assert adapter_config.gam_network_code == "444555666"


class TestAdminRoutes:
    """The admin write endpoints reject identity changes with a clean 403."""

    def test_update_adapter_switch_returns_403_when_locked(self, authenticated_admin_client, factory_session):
        tenant, _ = _gam_tenant("lock_route_switch")
        SyncJobFactory(tenant=tenant)

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/settings/adapter",
            json={"adapter": "mock"},
        )
        assert resp.status_code == 403
        assert "locked" in resp.get_json()["error"].lower()

    def test_update_adapter_edit_config_returns_403_when_locked(self, authenticated_admin_client, factory_session):
        tenant, _ = _gam_tenant("lock_route_edit")
        SyncJobFactory(tenant=tenant)

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/settings/adapter",
            json={"adapter": "google_ad_manager", "action": "edit_config"},
        )
        assert resp.status_code == 403

    def test_update_adapter_network_code_change_returns_403_when_locked(
        self, authenticated_admin_client, factory_session
    ):
        tenant, _ = _gam_tenant("lock_route_code")
        SyncJobFactory(tenant=tenant)

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/settings/adapter",
            json={"adapter": "google_ad_manager", "gam_network_code": "999888777"},
        )
        assert resp.status_code == 403

    def test_update_adapter_same_adapter_settings_allowed_when_locked(
        self, authenticated_admin_client, factory_session
    ):
        tenant, _ = _gam_tenant("lock_route_ok")
        SyncJobFactory(tenant=tenant)

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/settings/adapter",
            json={
                "adapter": "google_ad_manager",
                "gam_network_code": "111222333",
                "order_name_template": "Order {po_number}",
            },
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_update_adapter_switch_allowed_when_not_synced(self, authenticated_admin_client, factory_session):
        tenant, _ = _gam_tenant("lock_route_free")

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/settings/adapter",
            json={"adapter": "mock"},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_save_adapter_config_switch_returns_403_when_locked(self, authenticated_admin_client, factory_session):
        tenant, _ = _gam_tenant("lock_route_cfg")
        SyncJobFactory(tenant=tenant)

        resp = authenticated_admin_client.post(
            f"/api/tenant/{tenant.tenant_id}/adapter-config",
            json={"adapter_type": "mock", "config": {}},
        )
        assert resp.status_code == 403
        assert "locked" in resp.get_json()["error"].lower()

    def test_configure_gam_network_change_returns_403_when_locked(self, authenticated_admin_client, factory_session):
        tenant, _ = _gam_tenant("lock_route_gam")
        SyncJobFactory(tenant=tenant)

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/gam/configure",
            json={"auth_method": "oauth", "network_code": "999888777", "refresh_token": "tok_new"},
        )
        assert resp.status_code == 403

    def test_configure_gam_token_rotation_allowed_when_locked(self, authenticated_admin_client, factory_session):
        tenant, _ = _gam_tenant("lock_route_gam_rotate")
        SyncJobFactory(tenant=tenant)

        resp = authenticated_admin_client.post(
            f"/tenant/{tenant.tenant_id}/gam/configure",
            json={"auth_method": "oauth", "network_code": "111222333", "refresh_token": "tok_new"},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
