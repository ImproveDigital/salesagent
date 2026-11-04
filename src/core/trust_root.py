"""Builders for the public ``/.well-known`` trust-root documents.

Three documents, served per tenant host by
``core.middleware.well_known.WellKnownTrustRootMiddleware``:

* ``brand.json`` — House Portfolio variant. ``house`` comes from deployment
  env (``BRAND_HOUSE_DOMAIN`` / ``BRAND_HOUSE_NAME``); the tenant is the one
  brand; the sales agent sits in ``house.agents[]``. ``house.agents`` (not a
  top-level ``agents[]``) because the adcp resolver ignores top-level agents
  on a document that has a ``house`` object. Without ``BRAND_HOUSE_DOMAIN``
  the Brand Agent variant (top-level ``agents[]``) is served instead.
* ``jwks.json`` — the tenant's webhook-signing public keys.
* ``adagents.json`` — authorized properties whose ``publisher_domain`` is the
  host serving the document. Never fabricates an authorization: no backing
  record means no document (404).

Pure functions — no session, no request. Documents are validated against the
pinned adcp models but the hand-built dict is what gets served: the models
type ``url`` as ``AnyUrl``, which appends a trailing slash to a bare origin,
and buyers byte-compare ``agents[].url`` against the agent URL they
registered.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from adcp import get_adcp_spec_version
from adcp.types.generated_poc.brand_discovery import BrandDiscovery3 as LibraryBrandAgentDocument
from adcp.types.generated_poc.brand_discovery import BrandDiscovery4 as LibraryHousePortfolioDocument
from adcp.types.generated_poc.core.agent_signing_key import AgentSigningKey as LibraryAgentSigningKey

from src.services.agent_url_resolver import resolve_agent_url

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.core.database.models import AuthorizedProperty, Tenant, TenantSigningCredential

BRAND_JSON_PATH = "/.well-known/brand.json"
JWKS_PATH = "/.well-known/jwks.json"
ADAGENTS_JSON_PATH = "/.well-known/adagents.json"

_SCHEMA_BASE = f"https://adcontextprotocol.org/schemas/{get_adcp_spec_version()}"
_SALES_AGENT_TYPE = "sales"
_ID_INVALID_CHARS = re.compile(r"[^a-z0-9_]")


def house_from_env() -> dict[str, str] | None:
    """The deployment-wide ``house`` block, or None when not configured."""
    domain = os.environ.get("BRAND_HOUSE_DOMAIN", "").strip().lower()
    if not domain:
        return None
    name = os.environ.get("BRAND_HOUSE_NAME", "").strip() or domain
    return {"domain": domain, "name": name}


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _schema_id(value: str) -> str:
    """Fold a tenant subdomain into the ``^[a-z0-9_]+$`` id pattern."""
    return _ID_INVALID_CHARS.sub("_", value.lower())


def brand_json_url(tenant: Tenant) -> str | None:
    """Absolute brand.json URL advertised on ``get_adcp_capabilities.identity``.

    An explicit ``tenant.brand_json_url`` wins (operator publishes brand.json
    on their own domain); otherwise it is the one we serve on the agent's host.
    """
    if tenant.brand_json_url:
        return tenant.brand_json_url
    agent_url = resolve_agent_url(tenant)
    return f"{_origin(agent_url)}{BRAND_JSON_PATH}" if agent_url else None


def _rfc3339(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_updated(tenant: Tenant, keys: Sequence[TenantSigningCredential]) -> str | None:
    """Derived from stored timestamps, never the clock, so unchanged data gives an unchanged document."""
    moments = [tenant.updated_at] if tenant.updated_at else []
    moments += [m for key in keys for m in (key.created_at, key.rotated_out_at) if m is not None]
    return _rfc3339(max(moments)) if moments else None


def _published_jwk(key: TenantSigningCredential) -> dict[str, Any]:
    """The stored public JWK verbatim (``adcp.signing.keygen`` already emits the full member set)."""
    jwk = dict(key.public_jwk)
    LibraryAgentSigningKey.model_validate(jwk)
    return jwk


def build_brand_json(
    tenant: Tenant,
    agent_url: str,
    keys: Sequence[TenantSigningCredential],
    house: dict[str, str] | None,
) -> dict[str, Any]:
    """brand.json declaring *agent_url* as this tenant's sales agent."""
    brand_id = _schema_id(tenant.subdomain)
    agent = {
        "id": f"{brand_id}_sales"[:100],
        "type": _SALES_AGENT_TYPE,
        "url": agent_url,
        "jwks_uri": f"{_origin(agent_url)}{JWKS_PATH}",
        "description": f"{tenant.name} sales agent"[:500],
    }
    document: dict[str, Any] = {"$schema": f"{_SCHEMA_BASE}/brand.json", "version": "1.0"}
    if house is not None:
        document["house"] = {**house, "agents": [agent]}
        document["brands"] = [{"id": brand_id, "names": [{"en": tenant.name}]}]
        model: type[LibraryHousePortfolioDocument | LibraryBrandAgentDocument] = LibraryHousePortfolioDocument
    else:
        document["agents"] = [agent]
        model = LibraryBrandAgentDocument
    if last_updated := _last_updated(tenant, keys):
        document["last_updated"] = last_updated
    model.model_validate(document)
    return document


def build_jwks(keys: Sequence[TenantSigningCredential]) -> dict[str, Any]:
    """RFC 7517 key set at ``/.well-known/jwks.json``."""
    return {"keys": [_published_jwk(key) for key in keys]}


def _property_entry(prop: AuthorizedProperty) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "property_id": prop.property_id,
        "property_type": prop.property_type,
        "name": prop.name,
        "identifiers": prop.identifiers,
        "publisher_domain": prop.publisher_domain,
    }
    if prop.tags:
        entry["tags"] = list(prop.tags)
    return entry


def build_adagents_json(
    tenant: Tenant,
    agent_url: str,
    keys: Sequence[TenantSigningCredential],
    properties: Sequence[AuthorizedProperty],
) -> dict[str, Any] | None:
    """adagents.json for the properties hosted on the serving host, or None.

    *properties* must already be filtered to the serving host by the caller.
    None when there are none: the schema rejects a file carrying neither an
    authorization nor catalog content, so "nothing to claim" is a 404.
    The ``url`` is the agent origin — adagents.json matching canonicalizes
    rather than byte-compares.
    """
    if not properties:
        return None
    entry: dict[str, Any] = {
        "authorization_type": "inline_properties",
        "properties": [_property_entry(prop) for prop in properties],
        "url": _origin(agent_url),
        "authorized_for": f"Advertising inventory on properties operated by {tenant.name}"[:500],
    }
    if keys:
        entry["signing_keys"] = [_published_jwk(key) for key in keys]
    document: dict[str, Any] = {"$schema": f"{_SCHEMA_BASE}/adagents.json", "authorized_agents": [entry]}
    if last_updated := _last_updated(tenant, keys):
        document["last_updated"] = last_updated
    return document
