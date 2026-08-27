"""Per-page persistence of the Improve Digital inventory sync.

The sweep must upsert AND commit after every fetched page — not buffer the
whole family and write once at the end — so rows become visible/durable as
pagination progresses and an interrupted sweep keeps the pages it stored.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.adapters.improvedigital.inventory_sync import PAGE_SIZE, ImproveDigitalInventorySync

pytestmark = pytest.mark.unit


class TwoPagePlacementsClient:
    """Serves exactly two placement pages, then an empty page."""

    def __init__(self) -> None:
        total = PAGE_SIZE + 3  # forces a second, shorter page
        self._rows = [
            {"placement_id": i, "placement_name": f"pl-{i}", "publisher_id": 7, "publisher_name": "pub"}
            for i in range(total)
        ]
        self.inventory = MagicMock()
        self.inventory.search_placements.side_effect = self._page
        self.inventory.list_packages.return_value = {"packages": []}
        self.lookups = MagicMock()
        self.lookups.sizes.return_value = {"sizes": []}

    def _page(self, offset: int, limit: int) -> dict:
        return {
            "placements": self._rows[offset : offset + limit],
            "total_number_of_elements": len(self._rows),
        }


def make_sync(client) -> ImproveDigitalInventorySync:
    sync = ImproveDigitalInventorySync(client=client, session=MagicMock(), tenant_id="t1")
    sync._repo = MagicMock()
    return sync


class TestPerPagePersistence:
    def test_each_page_is_upserted_and_committed(self):
        client = TwoPagePlacementsClient()
        sync = make_sync(client)

        placement_count, publisher_count = sync._sync_placements()

        assert placement_count == PAGE_SIZE + 3
        assert publisher_count == 1
        # Two pages -> two upsert+commit rounds, interleaved with fetching,
        # never one buffered write at the end.
        assert sync._repo.bulk_upsert.call_count == 2
        assert sync._session.commit.call_count == 2

        first_batch = sync._repo.bulk_upsert.call_args_list[0].args[0]
        second_batch = sync._repo.bulk_upsert.call_args_list[1].args[0]
        # First page carries its placements plus the newly seen publisher;
        # the second page must not re-flush the same publisher.
        assert sum(1 for r in first_batch if r["entity_type"] == "placement") == PAGE_SIZE
        assert sum(1 for r in first_batch if r["entity_type"] == "publisher") == 1
        assert [r["entity_type"] for r in second_batch] == ["placement"] * 3

    def test_commit_follows_every_upsert(self):
        client = TwoPagePlacementsClient()
        sync = make_sync(client)
        order: list[str] = []
        sync._repo.bulk_upsert.side_effect = lambda rows: order.append(f"upsert:{len(rows)}")
        sync._session.commit.side_effect = lambda: order.append("commit")

        sync._sync_placements()

        assert order == [f"upsert:{PAGE_SIZE + 1}", "commit", "upsert:3", "commit"]

    def test_empty_pages_write_nothing(self):
        client = TwoPagePlacementsClient()
        sync = make_sync(client)

        assert sync._sync_packages() == 0
        assert sync._sync_sizes() == 0
        sync._repo.bulk_upsert.assert_not_called()
        sync._session.commit.assert_not_called()
