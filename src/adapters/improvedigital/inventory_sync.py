"""Inventory sync for the Improve Digital adapter — Phase 2 stub.

The full implementation (see ``docs/adapters/improvedigital/INTEGRATION_PLAN.md``
Phase 2) sweeps ``GET /rtb/v3/placements`` plus size/placement-type/package
lookups into an ``improvedigital_inventory`` cache table via a tenant-scoped
repository, and returns per-entity counts as an ``AdapterSyncResult``.

Until that lands, ``ImproveDigitalAdapter.capabilities.supports_inventory_sync``
stays ``False`` so the shared sync scheduler never invokes this path; any
direct call fails loudly below.
"""

from __future__ import annotations


class InventorySyncNotImplemented(RuntimeError):
    """Raised when the Phase 2 inventory sync is invoked before it exists."""

    def __init__(self) -> None:
        super().__init__(
            "Improve Digital inventory sync is not implemented yet — the placement "
            "cache (table, repository, migration, sweep) lands in Phase 2 of "
            "docs/adapters/improvedigital/INTEGRATION_PLAN.md. Flip "
            "AdapterCapabilities.supports_inventory_sync to True only alongside "
            "that implementation."
        )


class ImproveDigitalInventorySync:
    """Placeholder — constructing it fails loudly until Phase 2 lands."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise InventorySyncNotImplemented()
