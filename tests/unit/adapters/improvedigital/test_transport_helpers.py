"""Tests for the Improve Digital transport-support helpers."""

import pytest

from src.adapters.improvedigital._logging import safe_upstream_body_excerpt
from src.adapters.improvedigital._token_cache import BearerTokenCache

pytestmark = pytest.mark.unit


class TestBearerTokenCache:
    def test_requires_static_token_or_mint_fn(self):
        with pytest.raises(TypeError, match="static_token or mint_fn"):
            BearerTokenCache()

    def test_static_token_returned_verbatim(self):
        cache = BearerTokenCache(static_token="tok-static")
        assert cache.current() == "tok-static"
        assert cache.has_mint is False

    def test_mint_on_first_use_then_cached(self):
        calls = []

        def mint():
            calls.append(1)
            return f"tok-{len(calls)}", 600.0

        cache = BearerTokenCache(mint_fn=mint)
        assert cache.has_mint is True
        assert cache.current() == "tok-1"
        assert cache.current() == "tok-1"  # served from cache, no re-mint
        assert len(calls) == 1

    def test_expired_token_is_reminted(self, monkeypatch):
        now = {"t": 1000.0}
        monkeypatch.setattr("src.adapters.improvedigital._token_cache.time.time", lambda: now["t"])
        calls = []

        def mint():
            calls.append(1)
            return f"tok-{len(calls)}", 600.0

        cache = BearerTokenCache(mint_fn=mint, refresh_leeway_seconds=120.0)
        assert cache.current() == "tok-1"
        # expiry = 1000 + 600 - 120 = 1480; just before it, still cached
        now["t"] = 1479.0
        assert cache.current() == "tok-1"
        now["t"] = 1481.0
        assert cache.current() == "tok-2"
        assert len(calls) == 2

    def test_leeway_capped_at_half_ttl(self, monkeypatch):
        now = {"t": 0.0}
        monkeypatch.setattr("src.adapters.improvedigital._token_cache.time.time", lambda: now["t"])
        calls = []

        def mint():
            calls.append(1)
            return f"tok-{len(calls)}", 60.0  # short TTL: leeway capped at 30, not 300

        cache = BearerTokenCache(mint_fn=mint, refresh_leeway_seconds=300.0)
        assert cache.current() == "tok-1"
        now["t"] = 29.0  # expiry = 0 + 60 - 30 = 30
        assert cache.current() == "tok-1"
        now["t"] = 31.0
        assert cache.current() == "tok-2"

    def test_invalidate_forces_remint(self):
        calls = []

        def mint():
            calls.append(1)
            return f"tok-{len(calls)}", 600.0

        cache = BearerTokenCache(mint_fn=mint)
        assert cache.current() == "tok-1"
        cache.invalidate()
        assert cache.current() == "tok-2"


class TestSafeUpstreamBodyExcerpt:
    def test_empty_body_returns_empty_string(self):
        assert safe_upstream_body_excerpt(None) == ""
        assert safe_upstream_body_excerpt("") == ""

    def test_flattens_newlines_and_truncates(self):
        body = "line1\nline2\r" + "x" * 600
        excerpt = safe_upstream_body_excerpt(body, limit=20)
        assert "\n" not in excerpt and "\r" not in excerpt
        assert len(excerpt) == 20
        assert excerpt.startswith("line1 line2")

    def test_redacts_secret_like_fields(self):
        assert safe_upstream_body_excerpt('{"access_token": "abc"}') == (
            "<redacted: upstream body contained secret-like field>"
        )
        assert safe_upstream_body_excerpt("AUTHORIZATION: Bearer x") == (
            "<redacted: upstream body contained secret-like field>"
        )

    def test_plain_body_passes_through(self):
        assert safe_upstream_body_excerpt('{"error": "not found"}') == '{"error": "not found"}'
