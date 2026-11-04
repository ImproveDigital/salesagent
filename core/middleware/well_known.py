"""Serve the per-tenant ``/.well-known`` trust-root documents.

``GET /.well-known/brand.json``, ``/.well-known/jwks.json`` and
``/.well-known/adagents.json``. Unauthenticated by design: these documents
bootstrap trust, so they must be readable before any credential exists.
Registered in ``asgi_middleware`` ahead of the SDK's bearer auth, which only
ever sees requests this middleware passes through.

The tenant is resolved from the ``Host`` header with the same resolver the
SDK's ``SubdomainTenantMiddleware`` uses (injected as ``resolve_tenant`` so
this works in managed and test wirings where that middleware is absent). An
unknown host — or a tenant with no derivable agent URL — gets a 404.
Document shapes live in :mod:`src.core.trust_root`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from adcp.server.tenant_router import normalize_host_key
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from src.core.database.repositories.uow import TrustRootUoW
from src.core.trust_root import (
    ADAGENTS_JSON_PATH,
    BRAND_JSON_PATH,
    JWKS_PATH,
    agent_url_for_origin,
    build_adagents_json,
    build_brand_json,
    build_jwks,
)
from src.services.agent_url_resolver import resolve_agent_url

logger = logging.getLogger(__name__)

TRUST_ROOT_PATHS = frozenset({BRAND_JSON_PATH, JWKS_PATH, ADAGENTS_JSON_PATH})

#: Short enough that a key rotation reaches verifiers within minutes.
CACHE_MAX_AGE_SECONDS = 300

TenantResolver = Callable[[str], Awaitable[Any]]


def build_trust_root_document(tenant_id: str, path: str, host: str, origin: str) -> dict[str, Any] | None:
    """Build the document at *path* for *tenant_id*, or None (-> 404).

    *host* is the normalized serving host (adagents.json property filter);
    *origin* is the serving ``scheme://host[:port]`` (brand.json agent URL).
    """
    # Lazy: both modules pull in heavier import graphs than this middleware needs at load.
    from core.platforms._capabilities_envelope import WEBHOOK_DELIVERY_RETRY_HORIZON_SECONDS
    from src.services.webhook_signing import WEBHOOK_SIGNING_PURPOSE

    with TrustRootUoW(tenant_id) as uow:
        assert uow.tenant_config is not None
        assert uow.signing_credentials is not None
        tenant = uow.tenant_config.get_tenant()
        if tenant is None:
            return None
        # Rotated-out keys stay published for the whole retry horizon so a
        # webhook signed just before rotation still verifies.
        keys = uow.signing_credentials.list_publishable(
            WEBHOOK_SIGNING_PURPOSE,
            now=datetime.now(UTC),
            grace_seconds=WEBHOOK_DELIVERY_RETRY_HORIZON_SECONDS,
        )
        if path == JWKS_PATH:
            return build_jwks(keys)
        if path == BRAND_JSON_PATH:
            return build_brand_json(tenant, agent_url_for_origin(tenant, origin), keys)

        agent_url = resolve_agent_url(tenant)
        if not agent_url:
            logger.info("[TRUST-ROOT] tenant %s has no agent URL; %s not served", tenant_id, path)
            return None
        properties = [
            prop for prop in uow.tenant_config.list_authorized_properties() if prop.publisher_domain.lower() == host
        ]
        return build_adagents_json(tenant, agent_url, keys, properties)


class WellKnownTrustRootMiddleware:
    """Answer GET/HEAD on the trust-root paths; pass everything else through."""

    def __init__(self, app: ASGIApp, *, resolve_tenant: TenantResolver) -> None:
        self.app = app
        self.resolve_tenant = resolve_tenant

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("path") not in TRUST_ROOT_PATHS
            or scope.get("method") not in ("GET", "HEAD")
        ):
            await self.app(scope, receive, send)
            return

        response = await self._respond(scope)
        await response(scope, receive, send)

    async def _respond(self, scope: Scope) -> Response:
        not_found = JSONResponse({"detail": "Not found"}, status_code=404)
        raw_host = next((bytes(v).decode("latin-1") for k, v in scope.get("headers") or () if k == b"host"), "")
        if not raw_host:
            return not_found
        host = normalize_host_key(raw_host)

        tenant = await self.resolve_tenant(host)
        if tenant is None:
            logger.info("[TRUST-ROOT] no tenant for host %s", host)
            return not_found

        origin = f"{scope.get('scheme', 'https')}://{raw_host.strip().lower()}"
        document = await asyncio.to_thread(build_trust_root_document, tenant.id, scope["path"], host, origin)
        if document is None:
            return not_found
        return JSONResponse(document, headers={"Cache-Control": f"public, max-age={CACHE_MAX_AGE_SECONDS}"})
