"""Integration tests for the Improve Digital cache repositories.

Covers ImproveDigitalInventoryRepository (inventory taxonomy cache) and
ImproveDigitalLineItemStatsRepository (delivery stats cache), plus the
MediaBuyRepository.find_by_platform_line_item_id lookup they pair with.

Core invariant under test: every query is tenant-scoped by construction.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.harness._base import BareIntegrationEnv

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

AS_OF = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _setup_tenant(tenant_id: str):
    from tests.factories import TenantFactory

    return TenantFactory(tenant_id=tenant_id)


def _inventory_row(entity_type: str, entity_id: str, **overrides) -> dict:
    row = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "name": f"{entity_type} {entity_id}",
        "raw_json": {"id": entity_id},
    }
    row.update(overrides)
    return row


def _stats_row(line_item_id: str, **overrides) -> dict:
    row = {
        "line_item_id": line_item_id,
        "campaign_id": "camp_1",
        "impressions": 1000,
        "spend_micros": 5_000_000,
        "as_of": AS_OF,
    }
    row.update(overrides)
    return row


class TestInventoryRepository:
    def test_bulk_upsert_inserts_and_lists_by_type(self, integration_db):
        from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository

        with BareIntegrationEnv() as env:
            _setup_tenant("impd_t1")
            repo = ImproveDigitalInventoryRepository(env.get_session(), "impd_t1")

            touched = repo.bulk_upsert(
                [
                    _inventory_row("placement", "pl_1", parent_id="pub_1"),
                    _inventory_row("placement", "pl_2", parent_id="pub_2"),
                    _inventory_row("size", "300x250"),
                ]
            )

            assert touched == 3
            placements = repo.list_by_type("placement")
            assert {p.entity_id for p in placements} == {"pl_1", "pl_2"}
            assert [s.entity_id for s in repo.list_by_type("size")] == ["300x250"]

    def test_bulk_upsert_updates_existing_row_on_conflict(self, integration_db):
        from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository

        with BareIntegrationEnv() as env:
            _setup_tenant("impd_t1")
            repo = ImproveDigitalInventoryRepository(env.get_session(), "impd_t1")
            repo.bulk_upsert([_inventory_row("placement", "pl_1", name="Old Name")])

            repo.bulk_upsert([_inventory_row("placement", "pl_1", name="New Name", raw_json={"id": "pl_1", "v": 2})])

            rows = repo.list_by_type("placement")
            assert len(rows) == 1
            assert rows[0].name == "New Name"
            assert rows[0].raw_json == {"id": "pl_1", "v": 2}

    def test_bulk_upsert_empty_is_noop(self, integration_db):
        from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository

        with BareIntegrationEnv() as env:
            _setup_tenant("impd_t1")
            repo = ImproveDigitalInventoryRepository(env.get_session(), "impd_t1")
            assert repo.bulk_upsert([]) == 0

    def test_list_by_type_filters_by_parent(self, integration_db):
        from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository

        with BareIntegrationEnv() as env:
            _setup_tenant("impd_t1")
            repo = ImproveDigitalInventoryRepository(env.get_session(), "impd_t1")
            repo.bulk_upsert(
                [
                    _inventory_row("placement", "pl_1", parent_id="pub_1"),
                    _inventory_row("placement", "pl_2", parent_id="pub_2"),
                ]
            )

            children = repo.list_by_type("placement", parent_id="pub_1")
            assert [c.entity_id for c in children] == ["pl_1"]

    def test_search_matches_name_and_id_with_pagination(self, integration_db):
        from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository

        with BareIntegrationEnv() as env:
            _setup_tenant("impd_t1")
            repo = ImproveDigitalInventoryRepository(env.get_session(), "impd_t1")
            repo.bulk_upsert(
                [
                    _inventory_row("placement", "pl_1", name="Sports Banner"),
                    _inventory_row("placement", "pl_2", name="News Banner"),
                    _inventory_row("placement", "xyz_9", name="Homepage Video"),
                ]
            )

            by_name = repo.search("placement", q="banner")
            assert {r.entity_id for r in by_name} == {"pl_1", "pl_2"}

            by_id = repo.search("placement", q="xyz")
            assert [r.entity_id for r in by_id] == ["xyz_9"]

            page = repo.search("placement", q="banner", offset=1, limit=1)
            assert len(page) == 1

    def test_counts_by_type_and_latest_sync_at(self, integration_db):
        from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository

        with BareIntegrationEnv() as env:
            _setup_tenant("impd_t1")
            repo = ImproveDigitalInventoryRepository(env.get_session(), "impd_t1")
            assert repo.latest_sync_at() is None
            assert repo.counts_by_type() == {}

            repo.bulk_upsert(
                [
                    _inventory_row("placement", "pl_1", last_synced_at=AS_OF),
                    _inventory_row("placement", "pl_2", last_synced_at=AS_OF),
                    _inventory_row("size", "300x250", last_synced_at=AS_OF),
                ]
            )

            assert repo.counts_by_type() == {"placement": 2, "size": 1}
            assert repo.latest_sync_at() == AS_OF

    def test_delete_all_wipes_only_this_tenant(self, integration_db):
        from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository

        with BareIntegrationEnv() as env:
            _setup_tenant("impd_t1")
            _setup_tenant("impd_t2")
            session = env.get_session()
            repo_a = ImproveDigitalInventoryRepository(session, "impd_t1")
            repo_b = ImproveDigitalInventoryRepository(session, "impd_t2")
            repo_a.bulk_upsert([_inventory_row("placement", "pl_1")])
            repo_b.bulk_upsert([_inventory_row("placement", "pl_1")])

            deleted = repo_a.delete_all()

            assert deleted == 1
            assert repo_a.list_by_type("placement") == []
            assert [r.entity_id for r in repo_b.list_by_type("placement")] == ["pl_1"]

    def test_tenant_isolation_on_reads(self, integration_db):
        from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository

        with BareIntegrationEnv() as env:
            _setup_tenant("impd_t1")
            _setup_tenant("impd_t2")
            session = env.get_session()
            ImproveDigitalInventoryRepository(session, "impd_t2").bulk_upsert([_inventory_row("placement", "pl_9")])

            repo_a = ImproveDigitalInventoryRepository(session, "impd_t1")
            assert repo_a.list_by_type("placement") == []
            assert repo_a.search("placement", q="pl") == []


class TestLineItemStatsRepository:
    def test_bulk_upsert_inserts_and_updates(self, integration_db):
        from src.core.database.repositories.improvedigital_line_item_stats import (
            ImproveDigitalLineItemStatsRepository,
        )

        with BareIntegrationEnv() as env:
            _setup_tenant("impd_t1")
            repo = ImproveDigitalLineItemStatsRepository(env.get_session(), "impd_t1")

            assert repo.bulk_upsert([_stats_row("li_1"), _stats_row("li_2")]) == 2
            assert repo.bulk_upsert([_stats_row("li_1", impressions=2500, spend_micros=9_000_000)]) == 1

            rows = repo.get_by_line_item_ids(["li_1", "li_2"])
            assert rows["li_1"].impressions == 2500
            assert rows["li_1"].spend_micros == 9_000_000
            assert rows["li_2"].impressions == 1000

    def test_bulk_upsert_empty_is_noop(self, integration_db):
        from src.core.database.repositories.improvedigital_line_item_stats import (
            ImproveDigitalLineItemStatsRepository,
        )

        with BareIntegrationEnv() as env:
            _setup_tenant("impd_t1")
            repo = ImproveDigitalLineItemStatsRepository(env.get_session(), "impd_t1")
            assert repo.bulk_upsert([]) == 0

    def test_get_by_line_item_ids_omits_missing_and_handles_empty(self, integration_db):
        from src.core.database.repositories.improvedigital_line_item_stats import (
            ImproveDigitalLineItemStatsRepository,
        )

        with BareIntegrationEnv() as env:
            _setup_tenant("impd_t1")
            repo = ImproveDigitalLineItemStatsRepository(env.get_session(), "impd_t1")
            repo.bulk_upsert([_stats_row("li_1")])

            assert repo.get_by_line_item_ids([]) == {}
            result = repo.get_by_line_item_ids(["li_1", "li_missing"])
            assert set(result.keys()) == {"li_1"}

    def test_list_by_campaign_scopes_to_campaign(self, integration_db):
        from src.core.database.repositories.improvedigital_line_item_stats import (
            ImproveDigitalLineItemStatsRepository,
        )

        with BareIntegrationEnv() as env:
            _setup_tenant("impd_t1")
            repo = ImproveDigitalLineItemStatsRepository(env.get_session(), "impd_t1")
            repo.bulk_upsert(
                [
                    _stats_row("li_1", campaign_id="camp_1"),
                    _stats_row("li_2", campaign_id="camp_1"),
                    _stats_row("li_3", campaign_id="camp_2"),
                ]
            )

            rows = repo.list_by_campaign("camp_1")
            assert {r.line_item_id for r in rows} == {"li_1", "li_2"}

    def test_list_all_and_latest_sync_at(self, integration_db):
        from src.core.database.repositories.improvedigital_line_item_stats import (
            ImproveDigitalLineItemStatsRepository,
        )

        with BareIntegrationEnv() as env:
            _setup_tenant("impd_t1")
            repo = ImproveDigitalLineItemStatsRepository(env.get_session(), "impd_t1")
            assert repo.latest_sync_at() is None

            repo.bulk_upsert(
                [
                    _stats_row("li_1", campaign_id="camp_2", last_synced_at=AS_OF),
                    _stats_row("li_2", campaign_id="camp_1", last_synced_at=AS_OF),
                ]
            )

            rows = repo.list_all()
            assert [r.campaign_id for r in rows] == ["camp_2", "camp_1"]  # newest campaigns first
            assert repo.latest_sync_at() == AS_OF

    def test_tenant_isolation_on_reads(self, integration_db):
        from src.core.database.repositories.improvedigital_line_item_stats import (
            ImproveDigitalLineItemStatsRepository,
        )

        with BareIntegrationEnv() as env:
            _setup_tenant("impd_t1")
            _setup_tenant("impd_t2")
            session = env.get_session()
            ImproveDigitalLineItemStatsRepository(session, "impd_t2").bulk_upsert([_stats_row("li_1")])

            repo_a = ImproveDigitalLineItemStatsRepository(session, "impd_t1")
            assert repo_a.list_by_campaign("camp_1") == []
            assert repo_a.get_by_line_item_ids(["li_1"]) == {}


class TestFindByPlatformLineItemId:
    def test_resolves_buy_owning_the_platform_line_item(self, integration_db):
        from src.core.database.repositories.media_buy import MediaBuyRepository
        from tests.factories import MediaBuyFactory, MediaPackageFactory, PrincipalFactory, TenantFactory

        with BareIntegrationEnv() as env:
            tenant = TenantFactory(tenant_id="impd_t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="impd_p1")
            buy = MediaBuyFactory(tenant=tenant, principal=principal)
            MediaPackageFactory(
                media_buy=buy,
                package_config={"package_id": "pkg_1", "platform_line_item_id": 555455},
            )

            repo = MediaBuyRepository(env.get_session(), "impd_t1")

            # int-stored platform IDs resolve from string input (str coercion both sides)
            found = repo.find_by_platform_line_item_id("555455")
            assert found is not None
            assert found.media_buy_id == buy.media_buy_id

    def test_returns_none_when_no_package_matches(self, integration_db):
        from src.core.database.repositories.media_buy import MediaBuyRepository
        from tests.factories import MediaBuyFactory, MediaPackageFactory, PrincipalFactory, TenantFactory

        with BareIntegrationEnv() as env:
            tenant = TenantFactory(tenant_id="impd_t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="impd_p1")
            buy = MediaBuyFactory(tenant=tenant, principal=principal)
            MediaPackageFactory(media_buy=buy, package_config={"package_id": "pkg_1"})

            repo = MediaBuyRepository(env.get_session(), "impd_t1")
            assert repo.find_by_platform_line_item_id("999999") is None

    def test_scoped_to_tenant(self, integration_db):
        from src.core.database.repositories.media_buy import MediaBuyRepository
        from tests.factories import MediaBuyFactory, MediaPackageFactory, PrincipalFactory, TenantFactory

        with BareIntegrationEnv() as env:
            tenant_b = TenantFactory(tenant_id="impd_t2")
            principal_b = PrincipalFactory(tenant=tenant_b, principal_id="impd_p2")
            buy_b = MediaBuyFactory(tenant=tenant_b, principal=principal_b)
            MediaPackageFactory(
                media_buy=buy_b,
                package_config={"package_id": "pkg_1", "platform_line_item_id": "555455"},
            )
            TenantFactory(tenant_id="impd_t1")

            repo_a = MediaBuyRepository(env.get_session(), "impd_t1")
            assert repo_a.find_by_platform_line_item_id("555455") is None
