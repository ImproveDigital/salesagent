"""Inventory bundle authoring for an Improve Digital tenant.

Drives the bundle editor through the Flask test client against real
PostgreSQL: the picker API pages placements/packages out of the
``improvedigital_inventory`` cache, the editor embeds the synced creative
size catalogue (360Yield placements carry no sizes), and a bundle saves
with the formats derived from those sizes.
"""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from src.admin.app import create_app
from src.core.database.database_session import get_db_session
from src.core.database.models import ImproveDigitalInventory, InventoryProfile, Tenant
from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository
from tests.utils.database_helpers import create_tenant_with_timestamps

app = create_app()

pytestmark = [pytest.mark.admin, pytest.mark.requires_db]

_TENANT_ID = "inv_prof_impd_tenant"


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SESSION_COOKIE_PATH"] = "/"
    with app.test_client() as client:
        yield client


@pytest.fixture
def impd_tenant(integration_db):
    """Improve Digital tenant with a small synced inventory cache."""
    synced_at = datetime.now(UTC)
    with get_db_session() as session:
        session.execute(delete(InventoryProfile).where(InventoryProfile.tenant_id == _TENANT_ID))
        session.execute(delete(ImproveDigitalInventory).where(ImproveDigitalInventory.tenant_id == _TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.tenant_id == _TENANT_ID))
        session.commit()

        tenant = create_tenant_with_timestamps(
            tenant_id=_TENANT_ID,
            name="Improve Digital Bundle Tenant",
            subdomain="inv-prof-impd",
            ad_server="improvedigital",
            is_active=True,
        )
        session.add(tenant)
        session.commit()

        ImproveDigitalInventoryRepository(session, _TENANT_ID).bulk_upsert(
            [
                {
                    "entity_type": "publisher",
                    "entity_id": "7",
                    "name": "Jeep Community",
                    "parent_id": None,
                    "raw_json": {"id": 7, "name": "Jeep Community"},
                    "last_synced_at": synced_at,
                },
                {
                    "entity_type": "placement",
                    "entity_id": "23390686",
                    "name": "jeepcommunity.de-desktop-300x250",
                    "parent_id": "7",
                    "raw_json": {"placement_id": 23390686, "publisher_id": 7},
                    "last_synced_at": synced_at,
                },
                {
                    "entity_type": "placement",
                    "entity_id": "23366919",
                    "name": "ford-forum.de-mobile-300x250",
                    "parent_id": "7",
                    "raw_json": {"placement_id": 23366919, "publisher_id": 7},
                    "last_synced_at": synced_at,
                },
                {
                    "entity_type": "package",
                    "entity_id": "2832",
                    "name": "Automotive Premium",
                    "parent_id": None,
                    "raw_json": {"id": 2832, "name": "Automotive Premium", "sizes": []},
                    "last_synced_at": synced_at,
                },
                {
                    "entity_type": "size",
                    "entity_id": "171",
                    "name": "300x250",
                    "parent_id": None,
                    "raw_json": {"id": 171, "name": "300x250", "type": "display", "width": 300, "height": 250},
                    "last_synced_at": synced_at,
                },
                {
                    "entity_type": "size",
                    "entity_id": "893",
                    "name": "308x173 Video",
                    "parent_id": None,
                    "raw_json": {"id": 893, "name": "308x173 Video", "type": "vast", "width": 308, "height": 173},
                    "last_synced_at": synced_at,
                },
            ]
        )
        session.commit()
    return _TENANT_ID


def _auth_session(client, tenant_id):
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user"] = {"email": "test@example.com", "is_super_admin": True}
        sess["email"] = "test@example.com"
        sess["tenant_id"] = tenant_id
        sess["test_user"] = "test@example.com"
        sess["test_user_role"] = "super_admin"
        sess["test_user_name"] = "Test User"
        sess["test_tenant_id"] = tenant_id


class TestImproveDigitalBundleEditor:
    def test_create_form_uses_improvedigital_vocabulary_and_paged_picker(self, client, impd_tenant):
        _auth_session(client, impd_tenant)
        response = client.get(f"/tenant/{impd_tenant}/inventory-profiles/add")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "Flat placements" in html
        assert "INVENTORY_PICKER_PAGED = true" in html
        assert "INVENTORY_EXPLICIT_SIZES = true" in html
        # The synced size catalogue is embedded for the explicit size picker.
        assert '"label": "300x250"' in html
        assert '"kind": "video"' in html
        assert 'id="creative-size-picker"' in html

    def test_picker_api_pages_packages_and_placements(self, client, impd_tenant):
        _auth_session(client, impd_tenant)

        response = client.get(f"/tenant/{impd_tenant}/inventory-profiles/api/inventory?kind=placements")
        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["count"] == 1
        assert body["has_more"] is False
        assert [item["id"] for item in body["items"]] == ["2832"]
        assert body["items"][0]["kind"] == "placement"

        response = client.get(f"/tenant/{impd_tenant}/inventory-profiles/api/inventory?kind=ad_units&q=jeep&limit=1")
        assert response.status_code == 200
        body = response.get_json()
        assert body["count"] == 1
        assert body["items"][0]["id"] == "23390686"
        assert body["items"][0]["kind"] == "ad_unit"
        assert body["items"][0]["meta"] == "Jeep Community"
        assert body["items"][0]["bundle_count"] == 0

        response = client.get(f"/tenant/{impd_tenant}/inventory-profiles/api/inventory?kind=ad_units&limit=1")
        body = response.get_json()
        assert body["count"] == 2
        assert body["has_more"] is True

    def test_create_bundle_saves_placements_and_size_derived_formats(self, client, impd_tenant):
        _auth_session(client, impd_tenant)
        formats = [
            {"agent_url": "https://creative.adcontextprotocol.org", "id": "display_image", "width": 300, "height": 250},
            {"agent_url": "https://creative.adcontextprotocol.org", "id": "display_html", "width": 300, "height": 250},
        ]
        response = client.post(
            f"/tenant/{impd_tenant}/inventory-profiles/add",
            data={
                "name": "Automotive 300x250",
                "profile_id": "automotive_300x250",
                "description": "Created via test",
                "targeted_ad_unit_ids": json.dumps(["23390686", "23366919"]),
                "targeted_placement_ids": json.dumps(["2832"]),
                "formats": json.dumps(formats),
                "property_mode": "tags",
                "property_tags": "all_inventory",
            },
            follow_redirects=False,
        )
        assert response.status_code in (302, 303), response.get_data(as_text=True)[:500]

        with get_db_session() as session:
            profile = session.scalars(
                select(InventoryProfile).where(
                    InventoryProfile.tenant_id == impd_tenant,
                    InventoryProfile.profile_id == "automotive_300x250",
                )
            ).first()
            assert profile is not None
            assert profile.inventory_config["ad_units"] == ["23390686", "23366919"]
            assert profile.inventory_config["placements"] == ["2832"]
            assert profile.format_ids == formats
            profile_pk = profile.id

        # The edit page resolves the saved ids to names and re-embeds the sizes.
        response = client.get(f"/tenant/{impd_tenant}/inventory-profiles/{profile_pk}/edit")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "jeepcommunity.de-desktop-300x250" in html
        assert "Automotive Premium" in html
        assert "INVENTORY_EXPLICIT_SIZES = true" in html
