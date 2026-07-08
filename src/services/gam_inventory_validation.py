"""Validate a product's GAM inventory configuration against synced inventory.

A GAM product's targeted ad units / placements (its "Inventory Configuration")
must be consistent with the GAM inventory that has been synced into the local
``gam_inventory`` table. This module rejects products whose configuration is
inconsistent with the synced GAM settings:

- targeting no inventory at all,
- targeting ad units / placements that do not exist in synced inventory,
- targeting placements that are not ACTIVE in GAM, or
- declaring creative sizes that no targeted ad unit can serve.

The pure ``validate_gam_inventory_config`` function takes already-fetched
inventory rows so it can be unit-tested without a database. ``validate_product_gam_inventory``
is the DB-querying wrapper used by the admin product create/edit handlers.
"""

from __future__ import annotations

from sqlalchemy import func, select


def _ad_unit_sizes(row) -> set[tuple[int, int]]:
    """Return the (width, height) sizes declared by a GAMInventory ad unit row."""
    metadata = getattr(row, "inventory_metadata", None) or {}
    out: set[tuple[int, int]] = set()
    for size in metadata.get("sizes") or []:
        try:
            out.add((int(size["width"]), int(size["height"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _product_creative_sizes(implementation_config: dict) -> set[tuple[int, int]]:
    """Return the fixed display creative sizes declared by the product.

    Native placeholders and 1x1 sentinels are skipped — they have no fixed
    size to match against ad-unit sizes.
    """
    out: set[tuple[int, int]] = set()
    for placeholder in implementation_config.get("creative_placeholders") or []:
        if not isinstance(placeholder, dict) or placeholder.get("is_native"):
            continue
        try:
            width, height = int(placeholder["width"]), int(placeholder["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if width <= 1 or height <= 1:
            continue
        out.add((width, height))
    return out


def _format_sizes(sizes: set[tuple[int, int]]) -> str:
    return ", ".join(f"{w}x{h}" for w, h in sorted(sizes))


def validate_gam_inventory_config(
    implementation_config: dict,
    ad_unit_rows: list,
    placement_rows: list,
    *,
    synced_ad_unit_count: int | None = None,
    synced_placement_count: int | None = None,
) -> list[str]:
    """Return a list of consistency errors. An empty list means the config is valid.

    Args:
        implementation_config: the product's GAM implementation config, holding
            ``targeted_ad_unit_ids``, ``targeted_placement_ids`` and
            ``creative_placeholders``.
        ad_unit_rows: GAMInventory rows (type ``ad_unit``) found in synced
            inventory for the product's targeted ad unit IDs.
        placement_rows: GAMInventory rows (type ``placement``) found in synced
            inventory for the product's targeted placement IDs.
        synced_ad_unit_count: total ad units synced for the tenant. When ``0`` a
            missing ID means GAM inventory was never synced (a different, clearer
            error). ``None`` = unknown, fall back to the generic "not found".
        synced_placement_count: same as above, for placements.
    """
    errors: list[str] = []

    targeted_ad_unit_ids = [str(x) for x in (implementation_config.get("targeted_ad_unit_ids") or [])]
    targeted_placement_ids = [str(x) for x in (implementation_config.get("targeted_placement_ids") or [])]

    # 1. A GAM product must target some inventory.
    if not targeted_ad_unit_ids and not targeted_placement_ids:
        errors.append("Inventory configuration is incomplete: target at least one ad unit or placement.")
        return errors

    # 2. Targeted IDs must exist in synced GAM inventory.
    found_ad_unit_ids = {r.inventory_id for r in ad_unit_rows}
    missing_ad_units = [i for i in targeted_ad_unit_ids if i not in found_ad_unit_ids]
    if missing_ad_units:
        if synced_ad_unit_count == 0:
            errors.append(
                "GAM ad unit inventory has not been synced yet. Run a GAM inventory sync, "
                "then target existing ad units."
            )
        else:
            errors.append(
                f"Ad unit(s) not found in synced GAM inventory: {', '.join(missing_ad_units)}. "
                "Sync inventory or choose existing ad units."
            )

    found_placement_ids = {r.inventory_id for r in placement_rows}
    missing_placements = [i for i in targeted_placement_ids if i not in found_placement_ids]
    if missing_placements:
        if synced_placement_count == 0:
            errors.append(
                "GAM placement inventory has not been synced yet. Run a GAM inventory sync, "
                "then target existing placements."
            )
        else:
            errors.append(
                f"Placement(s) not found in synced GAM inventory: {', '.join(missing_placements)}. "
                "Sync inventory or choose existing placements."
            )

    # 3. Targeted placements must be ACTIVE in GAM.
    inactive_placements = [r.inventory_id for r in placement_rows if (r.status or "").upper() != "ACTIVE"]
    if inactive_placements:
        errors.append(
            f"Placement(s) are not ACTIVE in GAM: {', '.join(inactive_placements)}. "
            "Targeting archived or inactive placements is not allowed."
        )

    # 4. Creative sizes must be serveable by the targeted ad units. Skipped when
    #    any targeted ad unit declares no sizes (run-of-network serves any size).
    product_sizes = _product_creative_sizes(implementation_config)
    if product_sizes and ad_unit_rows:
        run_of_network = any(not _ad_unit_sizes(r) for r in ad_unit_rows)
        sized_units = [r for r in ad_unit_rows if _ad_unit_sizes(r)]
        if sized_units and not run_of_network:
            supported = set().union(*(_ad_unit_sizes(r) for r in sized_units))
            if product_sizes.isdisjoint(supported):
                errors.append(
                    f"Creative size mismatch: product creative size(s) ({_format_sizes(product_sizes)}) "
                    f"are not supported by any targeted ad unit (supported: {_format_sizes(supported)})."
                )

    return errors


def validate_product_gam_inventory(db_session, tenant_id: str, implementation_config: dict) -> list[str]:
    """Fetch synced inventory for the config's targeted IDs and validate consistency.

    Returns a list of human-readable error messages (empty = valid).
    """
    from src.core.database.models import GAMInventory

    ad_unit_ids = [str(x) for x in (implementation_config.get("targeted_ad_unit_ids") or [])]
    placement_ids = [str(x) for x in (implementation_config.get("targeted_placement_ids") or [])]

    def _synced_count(inventory_type: str) -> int:
        return (
            db_session.scalar(
                select(func.count())
                .select_from(GAMInventory)
                .filter(GAMInventory.tenant_id == tenant_id, GAMInventory.inventory_type == inventory_type)
            )
            or 0
        )

    ad_unit_rows: list = []
    synced_ad_unit_count: int | None = None
    if ad_unit_ids:
        ad_unit_rows = list(
            db_session.scalars(
                select(GAMInventory).filter(
                    GAMInventory.tenant_id == tenant_id,
                    GAMInventory.inventory_type == "ad_unit",
                    GAMInventory.inventory_id.in_(ad_unit_ids),
                )
            ).all()
        )
        # Only needed to distinguish "never synced" from "unknown ID" when some are missing.
        if len(ad_unit_rows) != len(set(ad_unit_ids)):
            synced_ad_unit_count = _synced_count("ad_unit")

    placement_rows: list = []
    synced_placement_count: int | None = None
    if placement_ids:
        placement_rows = list(
            db_session.scalars(
                select(GAMInventory).filter(
                    GAMInventory.tenant_id == tenant_id,
                    GAMInventory.inventory_type == "placement",
                    GAMInventory.inventory_id.in_(placement_ids),
                )
            ).all()
        )
        if len(placement_rows) != len(set(placement_ids)):
            synced_placement_count = _synced_count("placement")

    return validate_gam_inventory_config(
        implementation_config,
        ad_unit_rows,
        placement_rows,
        synced_ad_unit_count=synced_ad_unit_count,
        synced_placement_count=synced_placement_count,
    )
