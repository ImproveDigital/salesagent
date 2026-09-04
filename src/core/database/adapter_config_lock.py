"""Model-layer write guard freezing ad server identity after the first inventory sync.

Once a tenant has successfully synced inventory with its configured adapter, the
ad server identity — ``Tenant.ad_server``, ``AdapterConfig.adapter_type``, and
``AdapterConfig.gam_network_code`` — is locked. Switching adapters or GAM
networks after a sync would orphan the synced inventory, product implementation
configs, and media-buy history that reference the old network. A publisher who
needs a different ad server or network must create a new tenant.

Deliberately NOT locked (still writable after sync): credentials
(``gam_refresh_token``, service-account JSON, ``gam_auth_method``) so tokens can
be rotated for the same network, naming templates, approval flags,
currencies/timezone, AXE keys, and runtime caches (``custom_targeting_keys``,
``gam_sandbox_advertiser_id``).

Enforcement mirrors :mod:`src.core.database.embedded_tenant_guard`: SQLAlchemy
``before_update`` listeners diff the locked columns and raise
:class:`AdapterConfigLockedError` when a locked column's value actually changes
on a synced tenant. Same-value re-assignment (the settings forms resubmit the
stored network code) passes. Callers holding one of the embedded-guard auth
flags (``management_api_caller``, ``super_admin_override``,
``platform_background_worker``) bypass the lock — the Tenant Management API and
platform workers remain the ops escape hatch.

Inserts are not guarded: a synced tenant always already has its AdapterConfig
row, and the only delete-and-recreate path is the management API, which carries
an auth flag.

Importing this module attaches the listeners as a side effect; models.py
imports it at the bottom, next to embedded_tenant_guard.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import get_history

# Plain module import (not ``from ... import <name>``) — this module and
# embedded_tenant_guard are both imported from the bottom of models.py, so a
# name import would raise against the partially-loaded sibling module.
from src.core.database import embedded_tenant_guard as _embedded_guard
from src.core.database.models import AdapterConfig, GAMInventory, SyncJob, Tenant

# Terminal statuses that count as a successful sync — same vocabulary as
# SyncJobRepository.latest_success_for_stream().
_SUCCESS_STATUSES = ("completed", "success")

# Ad-server identity columns frozen after the first successful inventory sync.
LOCKED_TENANT_FIELDS: frozenset[str] = frozenset({"ad_server"})
LOCKED_ADAPTER_CONFIG_FIELDS: frozenset[str] = frozenset({"adapter_type", "gam_network_code"})

ADAPTER_LOCKED_MESSAGE = (
    "Ad server configuration is locked: inventory has already been synced with "
    "this ad server. To connect a different ad server or network, create a new tenant."
)


class AdapterConfigLockedError(Exception):
    """Raised when a locked ad-server identity field is changed on a synced tenant."""


def _tenant_has_synced_inventory(connection: Any, tenant_id: str | None) -> bool:
    """True when the tenant has a successful inventory sync on record.

    Two signals, matching what the rest of the codebase treats as "synced":
    a terminal-success ``sync_jobs`` row of ``sync_type='inventory'`` (any
    adapter), or — for tenants that synced before sync_jobs existed — the
    presence of ``gam_inventory`` rows (the setup-checklist signal).
    """
    if not tenant_id:
        return False

    sync_row = connection.execute(
        select(SyncJob.sync_id)
        .where(
            SyncJob.tenant_id == tenant_id,
            SyncJob.sync_type == "inventory",
            SyncJob.status.in_(_SUCCESS_STATUSES),
        )
        .limit(1)
    ).first()
    if sync_row is not None:
        return True

    inventory_row = connection.execute(
        select(GAMInventory.id).where(GAMInventory.tenant_id == tenant_id).limit(1)
    ).first()
    return inventory_row is not None


def is_adapter_config_locked(session: Session, tenant_id: str) -> bool:
    """Route/template-layer predicate for the same lock the listeners enforce."""
    return _tenant_has_synced_inventory(session.connection(), tenant_id)


def _locked_fields_actually_changed(connection: Any, target: Any, locked: frozenset[str]) -> list[str]:
    """Locked columns whose pending value differs from the stored DB value.

    Unlike the embedded guard, equality matters here: the adapter settings
    forms re-assign the current adapter/network code on every save, and a
    same-value write must not trip the lock. The comparison goes to the
    database (not attribute history) because a write to an expired/unloaded
    attribute carries no "old" value in its history.
    """
    pending: dict[str, Any] = {}
    for key in locked:
        history = get_history(target, key)
        if not history.has_changes():
            continue
        pending[key] = history.added[0] if history.added else None
    if not pending:
        return []

    cls = type(target)
    keys = sorted(pending)
    row = connection.execute(
        select(*(getattr(cls, key) for key in keys)).where(cls.tenant_id == target.tenant_id)
    ).first()
    if row is None:
        return keys

    stored = dict(zip(keys, row, strict=True))
    return [key for key in keys if pending[key] != stored[key]]


def _enforce_lock(connection: Any, target: Any, locked: frozenset[str]) -> None:
    changed = _locked_fields_actually_changed(connection, target, locked)
    if not changed:
        return

    if not _tenant_has_synced_inventory(connection, getattr(target, "tenant_id", None)):
        return

    if _embedded_guard._caller_is_authorized(target, connection):
        return

    raise AdapterConfigLockedError(
        f"{type(target).__name__} for tenant {getattr(target, 'tenant_id', '?')!r} "
        f"has synced inventory; ad server identity fields {changed} are locked. "
        f"{ADAPTER_LOCKED_MESSAGE}"
    )


@event.listens_for(Tenant, "before_update")
def _lock_tenant_ad_server(mapper, connection, target):
    _enforce_lock(connection, target, LOCKED_TENANT_FIELDS)


@event.listens_for(AdapterConfig, "before_update")
def _lock_adapter_config_identity(mapper, connection, target):
    _enforce_lock(connection, target, LOCKED_ADAPTER_CONFIG_FIELDS)
