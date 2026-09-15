"""Adapter framework for inventory-bundle authoring (#521).

The bundle list page, edit page, Reuse page, and the
``inventory_bundle_reference`` recompute were all GAM-shaped. This module
factors the GAM-specific bits behind a ``BundleInventoryAdapter`` protocol
so each ad server (GAM today, FreeWheel + SpringServe next) plugs in
without touching the UI or service layer.

The blueprint calls dispatch through ``get_adapter(adapter_id)`` and
``iter_adapters()`` to ask each adapter for coverage counts, unbundled
items, name resolution, seed suggestions, etc. The protocol returns
adapter-agnostic ``BundleInventoryRow`` records so templates don't need
to know which adapter owns a row.

GAM and Improve Digital are implemented. FW + SS are honest stubs — they return empty
results today (their inventory sync surfaces don't carry the same data
shape yet) but ship with their canonical labels + vocab so a tenant on
either of them sees the right copy in the page header.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalized row shape — what the blueprint and templates receive
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BundleInventoryRow:
    """One ad_unit or placement, adapter-agnostic.

    The blueprint reads ``external_id``, ``name``, ``entity_type``, and
    ``meta`` directly. ``raw`` carries adapter-specific extras (sizes for
    GAM ad units, etc.) for templates that opt in.
    """

    external_id: str
    name: str
    entity_type: str  # "ad_unit" | "placement"
    meta: str  # human-readable one-liner: "editorial · 300×250 · 2.1k imps/day"
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BundleInventoryAdapter(Protocol):
    """Read-only inventory surface that the bundle UI dispatches through.

    Implementations live next to each adapter's sync code. Register them
    at import time via :func:`register_adapter`.
    """

    adapter_id: str  # canonical id used in URL params, model columns, etc.
    label: str  # "Google Ad Manager"
    vocab: dict[str, str]  # {"primary": "ad units", "secondary": "placements"}
    matches_tenant_ad_server: set[str]  # values of ``tenant.ad_server`` to claim
    # True when the bundle editor should page inventory from the server
    # (via :meth:`search_inventory`) instead of embedding a bounded list.
    picker_paged: bool
    # True when the adapter's inventory rows carry no creative sizes, so the
    # bundle editor offers an explicit size picker (fed by
    # :meth:`list_creative_sizes`) instead of deriving formats from the
    # selected inventory.
    explicit_creative_sizes: bool

    def has_synced_inventory(self, session: Session, tenant_id: str) -> bool: ...

    def count_inventory(self, session: Session, tenant_id: str, entity_type: str) -> int: ...

    def list_inventory_by_ids(
        self, session: Session, tenant_id: str, entity_type: str, ids: list[str]
    ) -> list[BundleInventoryRow]: ...

    def list_inventory(
        self, session: Session, tenant_id: str, entity_type: str, limit: int | None = None
    ) -> list[BundleInventoryRow]: ...

    def list_unbundled(
        self,
        session: Session,
        tenant_id: str,
        bundled_ids_by_type: dict[str, set[str]],
        limit: int,
    ) -> list[BundleInventoryRow]: ...

    def list_top_level_placements(self, session: Session, tenant_id: str, limit: int) -> list[BundleInventoryRow]: ...

    def find_inventory_item(
        self, session: Session, tenant_id: str, entity_type: str, external_id: str
    ) -> BundleInventoryRow | None: ...

    def coverage_for_bundle(self, session: Session, tenant_id: str, inventory_config: dict) -> int: ...

    def search_inventory(
        self,
        session: Session,
        tenant_id: str,
        entity_type: str,
        *,
        q: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[BundleInventoryRow], int | None]:
        """One page of rows matching ``q`` plus the total match count
        (``None`` when the adapter cannot count cheaply)."""
        ...

    def list_creative_sizes(self, session: Session, tenant_id: str) -> list[dict[str, Any]]:
        """Options for the explicit creative-size picker — ``{label, width,
        height, kind}`` with ``kind`` ``display`` | ``video``. Empty for
        adapters that derive sizes from the selected inventory."""
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_ADAPTERS: dict[str, BundleInventoryAdapter] = {}


def register_adapter(adapter: BundleInventoryAdapter) -> None:
    """Register an adapter. Last write wins — useful for test overrides."""
    _ADAPTERS[adapter.adapter_id] = adapter


def get_adapter(adapter_id: str) -> BundleInventoryAdapter | None:
    """Look up a registered adapter by id, or ``None`` if unknown."""
    return _ADAPTERS.get(adapter_id)


def iter_adapters() -> list[BundleInventoryAdapter]:
    """All registered adapters, ordered by id for determinism."""
    return [_ADAPTERS[k] for k in sorted(_ADAPTERS.keys())]


def adapter_for_tenant(tenant_ad_server: str | None) -> BundleInventoryAdapter | None:
    """Resolve the adapter that claims this tenant's ``ad_server`` column.

    Returns ``None`` for tenants on ad servers no adapter has registered
    for (the bundle UI degrades to "no synced inventory yet" copy).
    """
    if not tenant_ad_server:
        return None
    for adapter in iter_adapters():
        if tenant_ad_server in adapter.matches_tenant_ad_server:
            return adapter
    return None


def _format_inventory_meta(name: str, path: list | None, status: str | None) -> str:
    """One-liner shared across adapter implementations.

    Mirrors the old ``_format_inventory_meta`` in the inventory_profiles
    blueprint — extracted here so each adapter renders meta consistently.
    """
    parts = []
    if path:
        parts.append(" › ".join(path[-3:]))
    if status and status.lower() != "active":
        parts.append(status.lower())
    return " · ".join(parts) if parts else "—"


# ---------------------------------------------------------------------------
# GAM adapter
# ---------------------------------------------------------------------------


class _GAMAdapter:
    """Google Ad Manager bundle-inventory adapter (#521).

    Delegates to ``GAMSyncRepository`` for all reads. The repository owns
    the SQL; this adapter just normalizes shape.
    """

    adapter_id = "gam"
    label = "Google Ad Manager"
    vocab = {"primary": "ad units", "secondary": "placements"}
    matches_tenant_ad_server = {"google_ad_manager", "gam"}
    picker_paged = False
    explicit_creative_sizes = False

    def _row_from_gam_inventory(self, row) -> BundleInventoryRow:
        return BundleInventoryRow(
            external_id=row.inventory_id,
            name=row.name,
            entity_type=row.inventory_type,
            meta=_format_inventory_meta(row.name, row.path, row.status),
            raw={"path": row.path, "status": row.status, "metadata": row.inventory_metadata},
        )

    def has_synced_inventory(self, session: Session, tenant_id: str) -> bool:
        return (
            self.count_inventory(session, tenant_id, "ad_unit") + self.count_inventory(session, tenant_id, "placement")
        ) > 0

    def count_inventory(self, session: Session, tenant_id: str, entity_type: str) -> int:
        from src.core.database.repositories.gam_sync import GAMSyncRepository

        return GAMSyncRepository(session, tenant_id).count_inventory(entity_type)

    def list_inventory_by_ids(
        self, session: Session, tenant_id: str, entity_type: str, ids: list[str]
    ) -> list[BundleInventoryRow]:
        from src.core.database.repositories.gam_sync import GAMSyncRepository

        rows = GAMSyncRepository(session, tenant_id).list_inventory_by_ids(entity_type, ids)
        return [self._row_from_gam_inventory(r) for r in rows]

    def list_inventory(
        self, session: Session, tenant_id: str, entity_type: str, limit: int | None = None
    ) -> list[BundleInventoryRow]:
        from src.core.database.repositories.gam_sync import GAMSyncRepository

        rows = GAMSyncRepository(session, tenant_id).list_inventory(entity_type, limit=limit)
        return [self._row_from_gam_inventory(r) for r in rows]

    def list_unbundled(
        self,
        session: Session,
        tenant_id: str,
        bundled_ids_by_type: dict[str, set[str]],
        limit: int,
    ) -> list[BundleInventoryRow]:
        from src.core.database.repositories.gam_sync import GAMSyncRepository

        rows = GAMSyncRepository(session, tenant_id).list_inventory_not_in_set(
            inventory_types=("ad_unit", "placement"),
            bundled_ids_by_type=bundled_ids_by_type,
            limit=limit,
        )
        return [self._row_from_gam_inventory(r) for r in rows]

    def list_top_level_placements(self, session: Session, tenant_id: str, limit: int) -> list[BundleInventoryRow]:
        from src.core.database.repositories.gam_sync import GAMSyncRepository

        rows = GAMSyncRepository(session, tenant_id).list_inventory("placement", limit=limit)
        return [self._row_from_gam_inventory(r) for r in rows]

    def find_inventory_item(
        self, session: Session, tenant_id: str, entity_type: str, external_id: str
    ) -> BundleInventoryRow | None:
        from src.core.database.repositories.gam_sync import GAMSyncRepository

        row = GAMSyncRepository(session, tenant_id).find_inventory_item(entity_type, external_id)
        return self._row_from_gam_inventory(row) if row else None

    def coverage_for_bundle(self, session: Session, tenant_id: str, inventory_config: dict) -> int:
        from src.core.database.repositories.gam_sync import GAMSyncRepository

        repo = GAMSyncRepository(session, tenant_id)
        direct_ad_unit_ids = set(inventory_config.get("ad_units") or [])
        placement_ids = list(inventory_config.get("placements") or [])
        covered = set(direct_ad_unit_ids)

        placements = repo.list_inventory_by_ids("placement", placement_ids)
        for placement in placements:
            metadata = placement.inventory_metadata or {}
            covered.update(str(ad_unit_id) for ad_unit_id in metadata.get("ad_unit_ids", []) if ad_unit_id)

        if not covered:
            return 0

        synced_ad_unit_ids = {row.inventory_id for row in repo.list_inventory("ad_unit")}
        return len(covered.intersection(synced_ad_unit_ids))

    def search_inventory(
        self,
        session: Session,
        tenant_id: str,
        entity_type: str,
        *,
        q: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[BundleInventoryRow], int | None]:
        from src.core.database.repositories.gam_sync import GAMSyncRepository

        repo = GAMSyncRepository(session, tenant_id)
        rows = repo.search_inventory(entity_type, q=q, offset=offset, limit=limit)
        total = repo.count_inventory(entity_type) if not q else None
        return [self._row_from_gam_inventory(r) for r in rows], total

    def list_creative_sizes(self, session: Session, tenant_id: str) -> list[dict[str, Any]]:
        return []


# ---------------------------------------------------------------------------
# Improve Digital adapter
# ---------------------------------------------------------------------------


class _ImproveDigitalAdapter:
    """Improve Digital (360Yield) bundle-inventory adapter.

    Reads the ``improvedigital_inventory`` cache filled by
    ``ImproveDigitalInventorySync``. The bundle model has two slots — a
    leaf entity (``ad_unit``) and a wrapper (``placement``) — which map onto
    360Yield's placements and placement packages respectively:

    * bundle ``ad_unit``   → cache ``placement`` (the bookable leaf)
    * bundle ``placement`` → cache ``package``   (reusable placement grouping)

    Package membership is not cached (it is a live per-package lookup), so
    packages never expand into child placements here and coverage counts
    only directly-picked placements.

    360Yield placements carry no creative sizes (sizes are a search filter
    and a separate ``size`` lookup), so ``explicit_creative_sizes`` is on:
    the editor offers the synced size catalogue and derives the bundle's
    canonical formats from the sizes the operator picks.

    ``picker_paged`` is on: the placement set runs to hundreds of thousands
    of rows, so the editor searches/pages through :meth:`search_inventory`
    instead of embedding a bounded list.
    """

    adapter_id = "improvedigital"
    label = "Improve Digital"
    vocab = {"primary": "placements", "secondary": "packages"}
    matches_tenant_ad_server = {"improvedigital", "improve_digital"}
    picker_paged = True
    explicit_creative_sizes = True

    _CACHE_ENTITY = {"ad_unit": "placement", "placement": "package"}
    # 360Yield size ``type`` → AdCP creative family. ``vast_audio`` has no
    # display/video equivalent and is skipped.
    _SIZE_KIND = {"display": "display", "mobile_app": "display", "text": "display", "vast": "video"}

    def _repo(self, session: Session, tenant_id: str):
        from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository

        return ImproveDigitalInventoryRepository(session, tenant_id)

    def _cache_type(self, entity_type: str) -> str:
        try:
            return self._CACHE_ENTITY[entity_type]
        except KeyError as exc:
            raise ValueError(f"Unknown bundle entity_type {entity_type!r}") from exc

    def _rows(
        self, repo, entity_type: str, tuples: list[tuple[str, str | None, str | None]]
    ) -> list[BundleInventoryRow]:
        """Normalize ``(entity_id, name, parent_id)`` tuples. Placements carry
        their publisher as ``parent_id``; one extra lookup labels them."""
        parent_ids = sorted({parent for _, _, parent in tuples if parent})
        publishers: dict[str, str | None] = {}
        if parent_ids:
            publishers = {pid: name for pid, name, _ in repo.list_picker_rows_by_ids("publisher", parent_ids)}
        rows: list[BundleInventoryRow] = []
        for entity_id, name, parent_id in tuples:
            publisher = publishers.get(parent_id) if parent_id else None
            rows.append(
                BundleInventoryRow(
                    external_id=str(entity_id),
                    name=name or str(entity_id),
                    entity_type=entity_type,
                    meta=publisher or "—",
                    raw={"metadata": {"parent_id": parent_id}, "publisher": publisher},
                )
            )
        return rows

    def has_synced_inventory(self, session: Session, tenant_id: str) -> bool:
        return (
            self.count_inventory(session, tenant_id, "ad_unit") + self.count_inventory(session, tenant_id, "placement")
        ) > 0

    def count_inventory(self, session: Session, tenant_id: str, entity_type: str) -> int:
        return self._repo(session, tenant_id).count_picker_rows(self._cache_type(entity_type))

    def list_inventory_by_ids(
        self, session: Session, tenant_id: str, entity_type: str, ids: list[str]
    ) -> list[BundleInventoryRow]:
        if not ids:
            return []
        repo = self._repo(session, tenant_id)
        return self._rows(repo, entity_type, repo.list_picker_rows_by_ids(self._cache_type(entity_type), ids))

    def list_inventory(
        self, session: Session, tenant_id: str, entity_type: str, limit: int | None = None
    ) -> list[BundleInventoryRow]:
        repo = self._repo(session, tenant_id)
        return self._rows(repo, entity_type, repo.list_picker_rows(self._cache_type(entity_type), limit=limit))

    def list_unbundled(
        self,
        session: Session,
        tenant_id: str,
        bundled_ids_by_type: dict[str, set[str]],
        limit: int,
    ) -> list[BundleInventoryRow]:
        # Packages first (they group placements), then placements fill the rest.
        repo = self._repo(session, tenant_id)
        out: list[BundleInventoryRow] = []
        for entity_type in ("placement", "ad_unit"):
            remaining = limit - len(out)
            if remaining <= 0:
                break
            tuples = repo.list_picker_rows(
                self._cache_type(entity_type),
                limit=remaining,
                exclude_ids=bundled_ids_by_type.get(entity_type) or (),
            )
            out.extend(self._rows(repo, entity_type, tuples))
        return out

    def list_top_level_placements(self, session: Session, tenant_id: str, limit: int) -> list[BundleInventoryRow]:
        return self.list_inventory(session, tenant_id, "placement", limit=limit)

    def find_inventory_item(
        self, session: Session, tenant_id: str, entity_type: str, external_id: str
    ) -> BundleInventoryRow | None:
        rows = self.list_inventory_by_ids(session, tenant_id, entity_type, [external_id])
        return rows[0] if rows else None

    def coverage_for_bundle(self, session: Session, tenant_id: str, inventory_config: dict) -> int:
        placement_ids = [str(i) for i in (inventory_config.get("ad_units") or []) if i]
        if not placement_ids:
            return 0
        return len(self._repo(session, tenant_id).list_picker_rows_by_ids("placement", placement_ids))

    def search_inventory(
        self,
        session: Session,
        tenant_id: str,
        entity_type: str,
        *,
        q: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[BundleInventoryRow], int | None]:
        repo = self._repo(session, tenant_id)
        cache_type = self._cache_type(entity_type)
        total = repo.count_picker_rows(cache_type, q=q)
        tuples = repo.list_picker_rows(cache_type, q=q, offset=offset, limit=limit)
        return self._rows(repo, entity_type, tuples), total

    def list_creative_sizes(self, session: Session, tenant_id: str) -> list[dict[str, Any]]:
        """Distinct ``(kind, width, height)`` options from the synced size
        catalogue (~800 rows, so loading the raw payloads is cheap). 1x1 and
        2x1 "text" placeholders have no AdCP display equivalent and are
        dropped."""
        options: dict[tuple[str, int, int], dict[str, Any]] = {}
        for row in self._repo(session, tenant_id).list_by_type("size"):
            raw = row.raw_json if isinstance(row.raw_json, dict) else {}
            kind = self._SIZE_KIND.get(str(raw.get("type") or "display"))
            width_raw, height_raw = raw.get("width"), raw.get("height")
            if kind is None or width_raw is None or height_raw is None:
                continue
            try:
                width, height = int(width_raw), int(height_raw)
            except (TypeError, ValueError):
                continue
            if width <= 1 or height <= 1:
                continue
            options.setdefault(
                (kind, width, height), {"label": f"{width}x{height}", "width": width, "height": height, "kind": kind}
            )
        return [options[key] for key in sorted(options)]


# ---------------------------------------------------------------------------
# FreeWheel + SpringServe stubs
# ---------------------------------------------------------------------------


class _NullInventoryAdapter:
    """Stub adapter for ad servers whose bundle-inventory surfaces aren't
    wired yet (FreeWheel, SpringServe).

    Returns empty results — the bundle UI degrades to "coverage strip
    hidden, no unbundled rail, no seed suggestions" but keeps adapter
    label + vocab visible so the page header reads correctly.

    When a real adapter lands, replace the stub via ``register_adapter``.
    """

    def __init__(self, *, adapter_id: str, label: str, vocab: dict[str, str], ad_server_aliases: set[str]):
        self.adapter_id = adapter_id
        self.label = label
        self.vocab = vocab
        self.matches_tenant_ad_server = ad_server_aliases
        self.picker_paged = False
        self.explicit_creative_sizes = False

    def has_synced_inventory(self, session: Session, tenant_id: str) -> bool:
        return False

    def count_inventory(self, session: Session, tenant_id: str, entity_type: str) -> int:
        return 0

    def list_inventory_by_ids(
        self, session: Session, tenant_id: str, entity_type: str, ids: list[str]
    ) -> list[BundleInventoryRow]:
        return []

    def list_inventory(
        self, session: Session, tenant_id: str, entity_type: str, limit: int | None = None
    ) -> list[BundleInventoryRow]:
        return []

    def list_unbundled(
        self,
        session: Session,
        tenant_id: str,
        bundled_ids_by_type: dict[str, set[str]],
        limit: int,
    ) -> list[BundleInventoryRow]:
        return []

    def list_top_level_placements(self, session: Session, tenant_id: str, limit: int) -> list[BundleInventoryRow]:
        return []

    def find_inventory_item(
        self, session: Session, tenant_id: str, entity_type: str, external_id: str
    ) -> BundleInventoryRow | None:
        return None

    def coverage_for_bundle(self, session: Session, tenant_id: str, inventory_config: dict) -> int:
        return 0

    def search_inventory(
        self,
        session: Session,
        tenant_id: str,
        entity_type: str,
        *,
        q: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[BundleInventoryRow], int | None]:
        return [], 0

    def list_creative_sizes(self, session: Session, tenant_id: str) -> list[dict[str, Any]]:
        return []


# ---------------------------------------------------------------------------
# Module-level registration
# ---------------------------------------------------------------------------

register_adapter(_GAMAdapter())
register_adapter(_ImproveDigitalAdapter())
register_adapter(
    _NullInventoryAdapter(
        adapter_id="freewheel",
        label="FreeWheel",
        # FreeWheel vocabulary: "placement groups" wrap "placements".
        vocab={"primary": "placements", "secondary": "placement groups"},
        ad_server_aliases={"freewheel", "fw"},
    )
)
register_adapter(
    _NullInventoryAdapter(
        adapter_id="springserve",
        label="SpringServe",
        # SpringServe vocabulary: tags + demand tags.
        vocab={"primary": "tags", "secondary": "demand tags"},
        ad_server_aliases={"springserve", "ss"},
    )
)
