"""Tests for the Improve Digital inventory sync — pagination, dedup, partial failure."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.adapters.improvedigital.inventory_sync import (
    PAGE_SIZE,
    ImproveDigitalInventorySync,
    SyncResult,
    _extract_items,
)

pytestmark = pytest.mark.unit


def make_sync(client: MagicMock) -> tuple[ImproveDigitalInventorySync, MagicMock]:
    session = MagicMock()
    sync = ImproveDigitalInventorySync(client=client, session=session, tenant_id="tenant_impd_1")
    return sync, session


def make_client(*, placements=None, packages=None, sizes=None) -> MagicMock:
    """Client whose list endpoints return a single short page each."""
    client = MagicMock()
    client.inventory.search_placements.return_value = {"placements": placements or []}
    client.inventory.list_packages.return_value = {"packages": packages or []}
    client.lookups.sizes.return_value = {"sizes": sizes or []}
    return client


class TestSyncResult:
    def test_total_and_success(self):
        result = SyncResult(counts={"placement": 3, "size": 2})
        assert result.total_synced == 5
        assert result.succeeded is True

    def test_errors_mean_failure(self):
        result = SyncResult(counts={"size": 2}, errors={"placement": "boom"})
        assert result.succeeded is False


class TestExtractItems:
    def test_prefers_documented_key(self):
        body = {"sizes": [{"id": 1}], "other": [{"id": 99}]}
        assert _extract_items(body, "sizes") == [{"id": 1}]

    def test_falls_back_to_first_list_value(self):
        body = {"totalNumberOfElemements": 1, "renamed_key": [{"id": 7}]}
        assert _extract_items(body, "sizes") == [{"id": 7}]

    def test_non_envelope_bodies_yield_nothing(self):
        assert _extract_items(None, "sizes") == []
        assert _extract_items({"count": 3}, "sizes") == []


class TestPlacementSync:
    def test_placements_and_publishers_derived(self):
        client = make_client(
            placements=[
                {"id": 11, "name": "Homepage ATF", "publisher_id": 5, "publisher_name": "Pub Five"},
                {"id": 12, "name": "Article BTF", "publisher_id": 5, "publisher_name": "Pub Five"},
                {"id": 13, "name": "Run of Site", "publisher_id": 6, "publisher_name": "Pub Six"},
            ]
        )
        sync, session = make_sync(client)
        result = sync.run()

        assert result.succeeded is True
        assert result.counts["placement"] == 3
        assert result.counts["publisher"] == 2  # deduped across placements
        assert session.execute.call_count >= 1

    def test_pagination_stops_on_short_page(self):
        full_page = [{"id": i, "name": f"p{i}", "publisher_id": 1, "publisher_name": "Pub"} for i in range(PAGE_SIZE)]
        short_page = [{"id": 9999, "name": "last", "publisher_id": 1, "publisher_name": "Pub"}]
        client = make_client()
        client.inventory.search_placements.side_effect = [
            {"placements": full_page, "totalNumberOfElemements": PAGE_SIZE + 1},
            {"placements": short_page, "totalNumberOfElemements": PAGE_SIZE + 1},
        ]
        sync, _session = make_sync(client)
        result = sync.run()

        assert result.counts["placement"] == PAGE_SIZE + 1
        assert client.inventory.search_placements.call_count == 2

    def test_rows_without_id_skipped(self):
        client = make_client(placements=[{"name": "no id"}, {"id": 1, "name": "ok"}])
        sync, _session = make_sync(client)
        result = sync.run()
        assert result.counts["placement"] == 1

    def test_live_v3_wire_shape(self):
        # Sandbox-confirmed: the v3 placement search returns placement_id /
        # placement_name / site_name and a snake_case total — the original
        # id/name mapping silently stored zero rows.
        client = make_client()
        client.inventory.search_placements.return_value = {
            "placements": [
                {
                    "placement_id": 22280001,
                    "placement_name": "Top Banner",
                    "site_name": "Example News Site",
                    "publisher_id": 1500,
                    "publisher_name": "Example Publisher BV",
                },
            ],
            "total_number_of_elements": 1,
        }
        sync, _session = make_sync(client)
        result = sync.run()

        assert result.counts["placement"] == 1
        assert result.counts["publisher"] == 1
        # snake_case total honored: one page, no second request
        assert client.inventory.search_placements.call_count == 1


class TestPartialFailure:
    def test_placement_failure_does_not_block_other_families(self):
        client = make_client(
            packages=[{"id": 3, "name": "Premium Bundle"}], sizes=[{"id": 4, "width": 300, "height": 250}]
        )
        client.inventory.search_placements.side_effect = RuntimeError("upstream 500")
        sync, _session = make_sync(client)
        result = sync.run()

        assert result.succeeded is False
        assert "upstream 500" in result.errors["placement"]
        assert result.counts["package"] == 1
        assert result.counts["size"] == 1
        assert "placement" not in result.counts


class TestUpsertRows:
    def test_size_name_falls_back_to_dimensions(self):
        client = make_client(
            sizes=[{"id": 4, "width": 300, "height": 250}, {"id": 5, "name": "Skin", "width": 1, "height": 1}]
        )
        sync, _session = make_sync(client)
        sync._repo = MagicMock()
        result = sync.run()

        assert result.counts["size"] == 2
        size_rows = [
            row
            for call in sync._repo.bulk_upsert.call_args_list
            for row in call.args[0]
            if row["entity_type"] == "size"
        ]
        by_id = {row["entity_id"]: row for row in size_rows}
        assert by_id["4"]["name"] == "300x250"
        assert by_id["5"]["name"] == "Skin"

    def test_placement_rows_carry_publisher_parent(self):
        client = make_client(
            placements=[{"id": 11, "name": "Homepage", "publisher_id": 5, "publisher_name": "Pub Five"}]
        )
        sync, _session = make_sync(client)
        sync._repo = MagicMock()
        sync.run()

        rows = [row for call in sync._repo.bulk_upsert.call_args_list for row in call.args[0]]
        placement = next(row for row in rows if row["entity_type"] == "placement")
        publisher = next(row for row in rows if row["entity_type"] == "publisher")
        assert placement["entity_id"] == "11"
        assert placement["parent_id"] == "5"
        assert publisher["entity_id"] == "5"
        assert publisher["name"] == "Pub Five"
