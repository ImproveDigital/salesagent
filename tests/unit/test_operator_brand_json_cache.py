"""Unit tests for OperatorBrandJsonCache.

PR 1 of [signing-non-embedded](../../../docs/design/signing-non-embedded.md).
The cache wraps :class:`adcp.signing.BrandJsonJwksResolver`; we don't re-test
the library's resolution semantics here. We verify:

* Resolvers are memoized per ``(brand_json_url, agent_type)``.
* Different ``agent_type`` for the same URL gets distinct resolvers.
* Different URLs get distinct resolvers.
* The singleton is module-stable.
"""

from __future__ import annotations

import asyncio

from src.core.signing import (
    OperatorBrandJsonCache,
    get_operator_brand_json_cache,
)


class TestOperatorBrandJsonCacheMemoization:
    def test_same_url_same_type_returns_same_resolver(self):
        cache = OperatorBrandJsonCache()
        url = "https://op.example.com/.well-known/brand.json"
        first = asyncio.run(cache.resolver_for(url))
        second = asyncio.run(cache.resolver_for(url))
        assert first is second

    def test_same_url_different_agent_type_returns_distinct_resolvers(self):
        cache = OperatorBrandJsonCache()
        url = "https://op.example.com/.well-known/brand.json"
        buying = asyncio.run(cache.resolver_for(url, agent_type="buying"))
        creative = asyncio.run(cache.resolver_for(url, agent_type="creative"))
        assert buying is not creative

    def test_different_urls_return_distinct_resolvers(self):
        cache = OperatorBrandJsonCache()
        a = asyncio.run(cache.resolver_for("https://a.example.com/.well-known/brand.json"))
        b = asyncio.run(cache.resolver_for("https://b.example.com/.well-known/brand.json"))
        assert a is not b

    def test_clear_drops_cached_resolvers(self):
        cache = OperatorBrandJsonCache()
        url = "https://op.example.com/.well-known/brand.json"
        first = asyncio.run(cache.resolver_for(url))
        cache.clear()
        second = asyncio.run(cache.resolver_for(url))
        assert first is not second


class TestSingleton:
    def test_get_returns_same_instance(self):
        a = get_operator_brand_json_cache()
        b = get_operator_brand_json_cache()
        assert a is b
