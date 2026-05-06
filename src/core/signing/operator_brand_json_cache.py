"""Per-process cache of ``BrandJsonJwksResolver`` instances.

PR 1 of [signing-non-embedded](../../../../docs/design/signing-non-embedded.md).

The library's :class:`adcp.signing.BrandJsonJwksResolver` carries its own
TTL-aware brand.json snapshot + inner JWKS cache; we just memoize the resolver
instance itself per ``(brand_json_url, agent_type)`` so concurrent verifies on
the same operator share one chain walk.

Threading note: the resolver is async-only. The cache is a plain dict guarded
by an asyncio lock at construction time; reads after construction are
lock-free because dict insert is GIL-atomic.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from adcp.signing import BrandJsonJwksResolver

if TYPE_CHECKING:
    from adcp.signing.brand_jwks import BrandAgentType

# (brand_json_url, agent_type) → resolver
_ResolverKey = tuple[str, str]


def _allow_private_destinations() -> bool:
    """Whether to permit private/loopback brand.json URLs.

    Defaults False (production-safe). Mirrors the webhook-delivery convention:
    set ``ADCP_AUTH_TEST_MODE=true`` or ``WEBHOOK_ALLOW_PRIVATE_IPS=true`` for
    dev/CI fixtures pointing at localhost mocks.
    """
    if os.getenv("ADCP_AUTH_TEST_MODE", "").lower() in ("1", "true", "yes"):
        return True
    if os.getenv("WEBHOOK_ALLOW_PRIVATE_IPS", "").lower() in ("1", "true", "yes"):
        return True
    return False


class OperatorBrandJsonCache:
    """Thin memoization layer over :class:`BrandJsonJwksResolver`.

    Construct one per process. Hand resolvers from :meth:`resolver_for` to
    :class:`adcp.signing.VerifyOptions` as the ``jwks_resolver`` dependency.
    """

    def __init__(self) -> None:
        self._resolvers: dict[_ResolverKey, BrandJsonJwksResolver] = {}
        self._lock = asyncio.Lock()

    async def resolver_for(
        self,
        brand_json_url: str,
        *,
        agent_type: BrandAgentType = "buying",
    ) -> BrandJsonJwksResolver:
        """Return the cached resolver for ``(brand_json_url, agent_type)``.

        Constructs lazily on first use. The library handles all caching, SSRF,
        and refresh logic from there on out.
        """
        key: _ResolverKey = (brand_json_url, agent_type)
        cached = self._resolvers.get(key)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._resolvers.get(key)
            if cached is not None:
                return cached
            resolver = BrandJsonJwksResolver(
                brand_json_url,
                agent_type=agent_type,
                allow_private_destinations=_allow_private_destinations(),
            )
            self._resolvers[key] = resolver
            return resolver

    def clear(self) -> None:
        """Drop all cached resolvers — primarily for tests."""
        self._resolvers.clear()


_singleton: OperatorBrandJsonCache | None = None


def get_operator_brand_json_cache() -> OperatorBrandJsonCache:
    """Return the process-wide cache singleton.

    Tests that need isolation should construct their own
    :class:`OperatorBrandJsonCache` directly rather than using the singleton.
    """
    global _singleton
    if _singleton is None:
        _singleton = OperatorBrandJsonCache()
    return _singleton
