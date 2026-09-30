"""Integration tests: list_creatives wire pagination/sort/assignments + read-only listing.

Regressions observed on the dev deployment (adcp 7.0.2, over MCP):

1. ``pagination.max_results`` / ``pagination.cursor`` / ``sort`` /
   ``include_assignments`` had no effect on the wire and the response carried
   no cursor, so every creative past the first page was unreachable.
2. Every listing showed ``updated_date`` bumped to "now" on creatives that
   were never updated (``updated_at`` NULL in the DB) — a read must neither
   write nor fabricate a modification timestamp.

Uses CreativeListEnv + real PostgreSQL + factory_boy. Persisted state is read
back through the repository layer (CreativeUoW), never a raw session.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.core.database.repositories.uow import CreativeUoW
from src.core.exceptions import AdCPValidationError
from tests.factories import (
    CreativeAssignmentFactory,
    CreativeFactory,
    MediaBuyFactory,
    PrincipalFactory,
    TenantFactory,
)
from tests.harness import CreativeListEnv

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

TENANT_ID = "test_tenant"
PRINCIPAL_ID = "test_principal"
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _seed_tenant():
    tenant = TenantFactory(tenant_id=TENANT_ID)
    principal = PrincipalFactory(tenant=tenant, principal_id=PRINCIPAL_ID)
    return tenant, principal


def _seed_creatives(count: int) -> list[str]:
    """Create ``count`` creatives with strictly increasing ``created_at``."""
    tenant, principal = _seed_tenant()
    return [
        CreativeFactory(
            tenant=tenant,
            principal=principal,
            creative_id=f"c_{i:03d}",
            created_at=BASE_TS + timedelta(minutes=i),
        ).creative_id
        for i in range(count)
    ]


def _db_updated_at() -> dict[str, datetime | None]:
    """Read ``updated_at`` for every creative of the test principal via the repository."""
    with CreativeUoW(TENANT_ID) as uow:
        assert uow.creatives is not None
        return {c.creative_id: c.updated_at for c in uow.creatives.list_by_principal(PRINCIPAL_ID)}


# ---------------------------------------------------------------------------
# Cursor pagination on the wire
# ---------------------------------------------------------------------------


class TestCursorPaginationOnWire:
    """``pagination.max_results`` + ``pagination.cursor`` drive paging over MCP."""

    def test_max_results_and_cursor_reach_every_creative_exactly_once(self, integration_db):
        with CreativeListEnv() as env:
            expected = set(_seed_creatives(60))

            seen: list[str] = []
            pages = 0
            cursor: str | None = None
            while True:
                pagination: dict[str, object] = {"max_results": 10}
                if cursor:
                    pagination["cursor"] = cursor
                response = env.call_mcp(pagination=pagination)
                pages += 1

                assert len(response.creatives) <= 10, "max_results ignored on the wire"
                assert response.pagination.total_count == 60
                seen.extend(c.creative_id for c in response.creatives)

                if not response.pagination.has_more:
                    assert response.pagination.cursor is None
                    break
                assert response.pagination.cursor, "has_more without a cursor leaves later pages unreachable"
                cursor = response.pagination.cursor
                assert pages < 20, "cursor never advanced"

        assert pages == 6
        assert len(seen) == 60
        assert len(set(seen)) == 60, "a creative was returned on more than one page"
        assert set(seen) == expected

    def test_cursor_pages_via_impl(self, integration_db):
        with CreativeListEnv() as env:
            _seed_creatives(25)
            first = env.call_impl(limit=10)
            second = env.call_impl(limit=10, cursor=first.pagination.cursor)
            third = env.call_impl(limit=10, cursor=second.pagination.cursor)

        assert [len(r.creatives) for r in (first, second, third)] == [10, 10, 5]
        assert first.pagination.has_more is True
        assert second.pagination.has_more is True
        assert third.pagination.has_more is False
        assert third.pagination.cursor is None

        ids = [c.creative_id for r in (first, second, third) for c in r.creatives]
        assert len(set(ids)) == 25
        # Default sort is created_date desc — newest first, oldest last.
        assert ids[0] == "c_024"
        assert ids[-1] == "c_000"

    def test_invalid_cursor_is_rejected(self, integration_db):
        with CreativeListEnv() as env:
            _seed_creatives(1)
            with pytest.raises(AdCPValidationError, match="cursor"):
                env.call_impl(cursor="not-a-cursor")

    def test_pages_do_not_overlap_when_created_at_ties(self, integration_db):
        """Equal ``created_at`` must still yield a total order across pages."""
        with CreativeListEnv() as env:
            tenant, principal = _seed_tenant()
            for i in range(12):
                CreativeFactory(tenant=tenant, principal=principal, creative_id=f"tie_{i:02d}", created_at=BASE_TS)

            seen: list[str] = []
            cursor: str | None = None
            for _ in range(4):
                response = env.call_impl(limit=5, cursor=cursor)
                seen.extend(c.creative_id for c in response.creatives)
                if not response.pagination.has_more:
                    break
                cursor = response.pagination.cursor

        assert len(seen) == 12
        assert sorted(seen) == [f"tie_{i:02d}" for i in range(12)]


# ---------------------------------------------------------------------------
# Sort on the wire
# ---------------------------------------------------------------------------


class TestSortOnWire:
    """``sort.field`` / ``sort.direction`` are honoured over MCP."""

    def test_sort_by_name_both_directions(self, integration_db):
        with CreativeListEnv() as env:
            tenant, principal = _seed_tenant()
            # Creation order deliberately differs from name order.
            for i, name in enumerate(["charlie", "alpha", "bravo"]):
                CreativeFactory(
                    tenant=tenant,
                    principal=principal,
                    creative_id=f"c_{i}",
                    name=name,
                    created_at=BASE_TS + timedelta(minutes=i),
                )

            asc = env.call_mcp(sort={"field": "name", "direction": "asc"})
            desc = env.call_mcp(sort={"field": "name", "direction": "desc"})

        assert [c.name for c in asc.creatives] == ["alpha", "bravo", "charlie"]
        assert [c.name for c in desc.creatives] == ["charlie", "bravo", "alpha"]
        assert asc.query_summary.sort_applied is not None
        assert asc.query_summary.sort_applied.model_dump(mode="json", exclude_none=True) == {
            "field": "name",
            "direction": "asc",
        }

    def test_sort_by_updated_date_falls_back_to_created_date(self, integration_db):
        with CreativeListEnv() as env:
            tenant, principal = _seed_tenant()
            CreativeFactory(
                tenant=tenant,
                principal=principal,
                creative_id="c_old_update",
                created_at=BASE_TS,
                updated_at=BASE_TS + timedelta(hours=10),
            )
            CreativeFactory(
                tenant=tenant,
                principal=principal,
                creative_id="c_recent_update",
                created_at=BASE_TS + timedelta(minutes=1),
                updated_at=BASE_TS + timedelta(hours=1),
            )
            CreativeFactory(
                tenant=tenant,
                principal=principal,
                creative_id="c_never_updated",
                created_at=BASE_TS + timedelta(minutes=2),
                updated_at=None,
            )

            asc = env.call_mcp(sort={"field": "updated_date", "direction": "asc"})
            desc = env.call_impl(sort_by="updated_date", sort_order="desc")

        assert [c.creative_id for c in asc.creatives] == ["c_never_updated", "c_recent_update", "c_old_update"]
        assert [c.creative_id for c in desc.creatives] == ["c_old_update", "c_recent_update", "c_never_updated"]


# ---------------------------------------------------------------------------
# include_assignments on the wire
# ---------------------------------------------------------------------------


class TestIncludeAssignmentsOnWire:
    def test_include_assignments_returns_assigned_packages(self, integration_db):
        with CreativeListEnv() as env:
            tenant, principal = _seed_tenant()
            assigned = CreativeFactory(tenant=tenant, principal=principal, creative_id="c_assigned")
            CreativeFactory(tenant=tenant, principal=principal, creative_id="c_unassigned")
            mb = MediaBuyFactory(tenant=tenant)
            CreativeAssignmentFactory(creative=assigned, media_buy=mb, package_id="pkg_wire_1")

            with_assignments = env.call_mcp(include_assignments=True)
            without_assignments = env.call_mcp(include_assignments=False)

        by_id = {c.creative_id: c for c in with_assignments.creatives}
        assigned_block = by_id["c_assigned"].assignments
        assert assigned_block is not None
        assert assigned_block.assignment_count == 1
        assert assigned_block.assigned_packages is not None
        assert [p.package_id for p in assigned_block.assigned_packages] == ["pkg_wire_1"]
        assert assigned_block.assigned_packages[0].assigned_date.tzinfo is not None

        unassigned_block = by_id["c_unassigned"].assignments
        assert unassigned_block is not None
        assert unassigned_block.assignment_count == 0

        assert all(c.assignments is None for c in without_assignments.creatives)


# ---------------------------------------------------------------------------
# Listing is read-only
# ---------------------------------------------------------------------------


class TestListingIsReadOnly:
    def test_listing_leaves_db_updated_at_untouched(self, integration_db):
        with CreativeListEnv() as env:
            tenant, principal = _seed_tenant()
            CreativeFactory(
                tenant=tenant,
                principal=principal,
                creative_id="c_touched",
                created_at=BASE_TS,
                updated_at=BASE_TS + timedelta(days=1),
            )
            CreativeFactory(
                tenant=tenant,
                principal=principal,
                creative_id="c_never_updated",
                created_at=BASE_TS,
                updated_at=None,
            )

            before = _db_updated_at()
            env.call_mcp()
            env.call_mcp(include_assignments=True, sort={"field": "updated_date", "direction": "desc"})
            env.call_impl(limit=1)
            after = _db_updated_at()

        assert before["c_touched"] == BASE_TS + timedelta(days=1)
        assert before["c_never_updated"] is None
        assert after == before

    def test_updated_date_is_not_fabricated_for_never_updated_creatives(self, integration_db):
        with CreativeListEnv() as env:
            tenant, principal = _seed_tenant()
            CreativeFactory(
                tenant=tenant,
                principal=principal,
                creative_id="c_never_updated",
                created_at=BASE_TS,
                updated_at=None,
            )
            CreativeFactory(
                tenant=tenant,
                principal=principal,
                creative_id="c_touched",
                created_at=BASE_TS,
                updated_at=BASE_TS + timedelta(days=1),
            )

            first = env.call_mcp()
            second = env.call_mcp()

        first_by_id = {c.creative_id: c.updated_date for c in first.creatives}
        second_by_id = {c.creative_id: c.updated_date for c in second.creatives}

        # A creative that was never modified was last "updated" when it was created.
        assert first_by_id["c_never_updated"] == BASE_TS
        assert first_by_id["c_touched"] == BASE_TS + timedelta(days=1)
        # Two reads of unchanged rows must agree — no "now" leaking in.
        assert second_by_id == first_by_id
