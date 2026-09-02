"""Inventory sync for the Improve Digital adapter.

Sweeps the 360Yield buy-side inventory surfaces into the
``improvedigital_inventory`` cache:

- ``placement`` — paginated ``GET /rtb/v3/placements``; each placement's
  publisher becomes its ``parent_id``
- ``publisher`` — derived from the placement rows (``publisher_id`` /
  ``publisher_name`` are inline; there is no buy-side publishers endpoint)
- ``package``   — ``GET /rtb/v1/packages`` (reusable placement groupings)
- ``size``      — ``GET /rtb/v1/sizes-all`` (creative sizes)

Each entity family syncs independently (partial-success policy: one family
failing is recorded in ``SyncResult.errors``, the rest still land). Writes
go through :class:`ImproveDigitalInventoryRepository.bulk_upsert` and are
committed **per fetched page**, so rows become visible/durable as the sweep
progresses and an interrupted sweep keeps the pages it already stored.

List envelopes vary per endpoint (``SizesDto.sizes``, generic ``content``),
so item extraction is tolerant: known keys first, then the first list value
in the body.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from src.adapters.improvedigital.client import ImproveDigitalClient
from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository

logger = logging.getLogger(__name__)

# Page size for offset/limit pagination. Validated live: the placement
# search serves 1000-row (and larger) pages; big pages keep the sweep well
# under the API's 100-reads-per-60s rate limit (~25 pages for ~25k dev
# placements vs ~125 at the previous 200).
PAGE_SIZE = 1000

# Hard ceiling on pages per family — a runaway-pagination backstop, not a
# coverage limit (1000 * 500 = 500k rows per family).
MAX_PAGES = 500


@dataclass
class SyncResult:
    """Outcome of one inventory sync run."""

    counts: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def total_synced(self) -> int:
        return sum(self.counts.values())

    @property
    def succeeded(self) -> bool:
        return not self.errors


def _extract_items(body: Any, *preferred_keys: str) -> list[dict[str, Any]]:
    """Pull the item list out of a 360Yield list envelope.

    Tries the endpoint's documented key(s) first, then falls back to the
    first list-valued field so an upstream envelope rename degrades to a
    warning instead of a silent zero-row sync.
    """
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if not isinstance(body, dict):
        return []
    for key in preferred_keys:
        value = body.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for key, value in body.items():
        if isinstance(value, list) and (not value or isinstance(value[0], dict)):
            logger.warning("Improve Digital list envelope key fallback: expected %s, used %r", preferred_keys, key)
            return [item for item in value if isinstance(item, dict)]
    return []


def _row_id(item: dict[str, Any]) -> Any:
    """Entity id under generic or endpoint-prefixed keys (the v3 placement
    search uses ``placement_id`` — sandbox-confirmed)."""
    for key in ("id", "placement_id", "package_id", "size_id"):
        if item.get(key) is not None:
            return item[key]
    return None


def _reported_total(body: Any) -> int | None:
    """Total row count from either envelope spelling: the v1 endpoints use
    ``totalNumberOfElemements`` (sic), the v3 placement search uses
    ``total_number_of_elements``."""
    if not isinstance(body, dict):
        return None
    for key in ("totalNumberOfElemements", "total_number_of_elements"):
        value = body.get(key)
        if isinstance(value, int):
            return value
    return None


class ImproveDigitalInventorySync:
    """Walks the 360Yield inventory surfaces into the local cache."""

    def __init__(self, client: ImproveDigitalClient, session: Session, tenant_id: str):
        self._client = client
        self._session = session
        self._repo = ImproveDigitalInventoryRepository(session, tenant_id)

    def _persist_page(self, rows: list[dict[str, Any]]) -> None:
        """Upsert one page's rows and commit immediately, so inventory
        lands in the DB as each page is fetched rather than after the
        whole sweep."""
        if not rows:
            return
        self._repo.bulk_upsert(rows)
        self._session.commit()
        logger.info("Improve Digital inventory sync: upserted + committed %d rows", len(rows))

    def run(self) -> SyncResult:
        """Sync every entity family; record per-family errors, never raise."""
        result = SyncResult(started_at=datetime.now(UTC))

        try:
            placement_count, publisher_count = self._sync_placements()
            result.counts["placement"] = placement_count
            result.counts["publisher"] = publisher_count
        except Exception as exc:
            logger.warning("Improve Digital placement sync failed", exc_info=True)
            result.errors["placement"] = str(exc)

        for entity_type, sync_fn in (
            ("package", self._sync_packages),
            ("size", self._sync_sizes),
        ):
            try:
                result.counts[entity_type] = sync_fn()
            except Exception as exc:
                logger.warning("Improve Digital %s sync failed", entity_type, exc_info=True)
                result.errors[entity_type] = str(exc)

        result.finished_at = datetime.now(UTC)
        return result

    # ----- entity families -----

    def _sync_placements(self) -> tuple[int, int]:
        """Sweep ``GET /rtb/v3/placements`` and derive publishers.

        Each page is upserted and committed as it arrives; a publisher row
        is flushed with the first page that mentions it.

        Returns ``(placement_count, publisher_count)``.
        """
        synced_at = datetime.now(UTC)
        placement_count = 0
        publishers: dict[str, dict[str, Any]] = {}

        for page in self._iter_pages(
            lambda offset, limit: self._client.inventory.search_placements(offset=offset, limit=limit),
            "placements",
            "content",
        ):
            placement_rows: list[dict[str, Any]] = []
            new_publishers: list[dict[str, Any]] = []
            for item in page:
                # The live v3 search returns prefixed keys (placement_id,
                # placement_name, site_name) — sandbox-confirmed; the generic
                # id/name spelling is kept as a fallback.
                placement_id = item.get("placement_id") if item.get("placement_id") is not None else item.get("id")
                if placement_id is None:
                    continue
                name = item.get("placement_name") or item.get("name")
                site = item.get("site_name")
                # Placement names usually already embed their site; only append it
                # when it adds information, so pickers don't show "X-300x250 — X".
                display_name: str | None
                if name and site and site not in name:
                    display_name = f"{name} — {site}"
                else:
                    display_name = name or site
                publisher_id = item.get("publisher_id")
                placement_rows.append(
                    {
                        "entity_type": "placement",
                        "entity_id": str(placement_id),
                        "name": display_name,
                        "parent_id": str(publisher_id) if publisher_id is not None else None,
                        "raw_json": item,
                        "last_synced_at": synced_at,
                    }
                )
                if publisher_id is not None and str(publisher_id) not in publishers:
                    # First-write wins; every placement of a publisher carries
                    # the same inline name.
                    publisher_row = {
                        "entity_type": "publisher",
                        "entity_id": str(publisher_id),
                        "name": item.get("publisher_name"),
                        "parent_id": None,
                        "raw_json": {"id": publisher_id, "name": item.get("publisher_name")},
                        "last_synced_at": synced_at,
                    }
                    publishers[str(publisher_id)] = publisher_row
                    new_publishers.append(publisher_row)

            # Publishers flush with their page so stored placements never
            # point at a parent that hasn't landed yet.
            self._persist_page(placement_rows + new_publishers)
            placement_count += len(placement_rows)

        return placement_count, len(publishers)

    def _sync_packages(self) -> int:
        synced_at = datetime.now(UTC)
        count = 0
        for page in self._iter_pages(
            lambda offset, limit: self._client.inventory.list_packages(offset=offset, limit=limit),
            # Live envelope key since ~2026-08 is line_item_packages (observed
            # on dev; previously documented as packages — kept as fallbacks).
            "line_item_packages",
            "packages",
            "content",
        ):
            rows = [
                {
                    "entity_type": "package",
                    "entity_id": str(item["id"]),
                    "name": item.get("name"),
                    "parent_id": None,
                    "raw_json": item,
                    "last_synced_at": synced_at,
                }
                for item in page
                if item.get("id") is not None
            ]
            self._persist_page(rows)
            count += len(rows)
        return count

    def _sync_sizes(self) -> int:
        synced_at = datetime.now(UTC)
        count = 0
        for page in self._iter_pages(
            lambda offset, limit: self._client.lookups.sizes(offset=offset, limit=limit),
            "sizes",
        ):
            rows: list[dict[str, Any]] = []
            for item in page:
                if item.get("id") is None:
                    continue
                width, height = item.get("width"), item.get("height")
                dimension = f"{width}x{height}" if width is not None and height is not None else None
                rows.append(
                    {
                        "entity_type": "size",
                        "entity_id": str(item["id"]),
                        "name": item.get("name") or dimension,
                        "parent_id": None,
                        "raw_json": item,
                        "last_synced_at": synced_at,
                    }
                )
            self._persist_page(rows)
            count += len(rows)
        return count

    # ----- pagination -----

    def _iter_pages(self, fetch, *item_keys: str) -> Iterator[list[dict[str, Any]]]:
        """Yield each page's unseen items as one batch.

        Page-shaped (rather than item-shaped) so callers can persist and
        commit page by page — rows land in the DB as they are fetched.

        The server may clamp ``limit``, so a page shorter than the
        requested size does NOT mean it was the last one. Stops on an
        empty page, on a page with no unseen ids at all (a server ignoring
        ``offset`` would otherwise loop forever), when the reported
        ``totalNumberOfElemements`` (sic — upstream spelling) is reached,
        or at the MAX_PAGES backstop. A page that is only *partially*
        stale keeps the sweep going — offset windows drift when inventory
        changes mid-sweep, and one boundary duplicate must not truncate
        the remaining pages. Already-seen rows are not re-yielded, and any
        early stop short of the reported total is logged loudly.
        """
        offset = 0
        seen_ids: set[Any] = set()
        for _page in range(MAX_PAGES):
            body = fetch(offset, PAGE_SIZE)
            items = _extract_items(body, *item_keys)
            fresh = [item for item in items if _row_id(item) is None or _row_id(item) not in seen_ids]
            seen_ids.update(_row_id(item) for item in fresh if _row_id(item) is not None)
            if fresh:
                yield fresh

            total = _reported_total(body)
            offset += len(items)
            if not items or not fresh:
                if isinstance(total, int) and offset < total:
                    logger.warning(
                        "Improve Digital pagination stopped early at offset %s of reported total %s "
                        "(page had no unseen rows)",
                        offset,
                        total,
                    )
                return
            if isinstance(total, int) and offset >= total:
                return
        logger.warning("Improve Digital pagination hit the MAX_PAGES backstop at offset %s", offset)
