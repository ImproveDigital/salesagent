"""Model-layer write guard freezing the ad server connection identity after the first inventory sync.

Once a tenant's first inventory sync completes, the fields that identify the ad
server connection are locked:

- ``Tenant.ad_server`` and ``AdapterConfig.adapter_type`` — the adapter choice
- ``AdapterConfig.gam_network_code`` — the GAM network
- ``client_id`` / ``client_secret`` inside ``config_json`` — the network seat
  for schema-driven adapters (Improve Digital, FreeWheel API-Access)

Changing any of these after a sync would orphan the synced inventory, product
implementation configs, and media-buy history that reference the old network. A
publisher who needs a different ad server or network must create a new tenant.
Everything else on the adapter configuration (credentials such as the GAM
refresh token or FreeWheel password, naming templates, AXE keys, approval
flags, other ``config_json`` fields) stays editable.

Lock state is stored explicitly in ``adapter_config.config_locked_at``
(adapter-agnostic — every inventory-sync completion path stamps it via
:meth:`AdapterConfigRepository.mark_config_locked`, and the migration
backfilled already-synced tenants). ``config_locked_at`` is itself a locked
column: stamping it on an unlocked tenant is free, but clearing it once set
requires ``super_admin_override`` — that is the documented unlock procedure.

Enforcement mirrors :mod:`src.core.database.embedded_tenant_guard`: SQLAlchemy
``before_update`` listeners compare each locked column's pending value against
the stored DB value and raise :class:`AdapterConfigLockedError` on a real
change. Same-value re-assignment (the settings forms resubmit stored values on
every save) passes. Callers holding one of the embedded-guard auth flags
(``management_api_caller``, ``super_admin_override``,
``platform_background_worker``) bypass the lock — the Tenant Management API and
platform workers remain the ops escape hatch.

The ``client_id`` / ``client_secret`` keys inside ``config_json`` are enforced
by the ``save_adapter_config`` route rather than these listeners:
``client_secret`` is Fernet-encrypted at rest (non-deterministic ciphertext),
so only the route — which holds the schema that decrypts it — can compare
values meaningfully. The route is the sole UI write path for ``config_json``;
non-UI writers (management API, workers) carry auth flags anyway.

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
from src.core.database.models import AdapterConfig, Tenant

# Connection-identity columns frozen while config_locked_at is set. The lock
# stamp itself is in the set so that clearing it (unlocking) requires
# super_admin_override; stamping it on an unlocked tenant is unaffected.
LOCKED_TENANT_FIELDS: frozenset[str] = frozenset({"ad_server"})
LOCKED_ADAPTER_CONFIG_FIELDS: frozenset[str] = frozenset({"adapter_type", "gam_network_code", "config_locked_at"})

# config_json keys enforced by the save_adapter_config route (see module
# docstring for why these can't be checked at the listener level). api_base_url
# is the 360Yield host (production vs dev seat) — as much a part of the
# connection identity as the credentials themselves.
LOCKED_CONFIG_JSON_FIELDS: tuple[str, ...] = ("client_id", "client_secret", "api_base_url")

ADAPTER_LOCKED_MESSAGE = (
    "Ad server configuration is locked: inventory has already been synced with "
    "this ad server. The ad server, network code, API base URL, and client "
    "credentials cannot be changed. To connect a different ad server or network, "
    "create a new tenant."
)


class AdapterConfigLockedError(Exception):
    """Raised when a locked ad-server identity field is changed on a synced tenant."""


def _tenant_is_locked(connection: Any, tenant_id: str | None) -> bool:
    """True when the tenant's stored lock stamp is set.

    ``adapter_config.config_locked_at`` is the single source of truth: the
    inventory-sync completion paths stamp it via
    :meth:`AdapterConfigRepository.mark_config_locked`, and the migration
    backfilled tenants that had already synced. Clearing it (platform ops,
    under ``super_admin_override``) unlocks the tenant.
    """
    if not tenant_id:
        return False

    locked_at = connection.execute(
        select(AdapterConfig.config_locked_at).where(AdapterConfig.tenant_id == tenant_id)
    ).scalar()
    return locked_at is not None


def is_adapter_config_locked(session: Session, tenant_id: str) -> bool:
    """Route/template-layer predicate for the same lock the listeners enforce."""
    return _tenant_is_locked(session.connection(), tenant_id)


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

    if not _tenant_is_locked(connection, getattr(target, "tenant_id", None)):
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
