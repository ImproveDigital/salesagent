"""Public ``/.well-known`` trust-root documents: brand.json, jwks.json, adagents.json.

Builders are graded against the pinned adcp JSON schemas, and brand.json
additionally against the SDK resolver's own agent selector — the code a
buyer runs to find our ``agents[]`` entry.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import adcp
import jsonschema
import pytest
from adcp.decisioning import DecisioningCapabilities
from adcp.signing.brand_jwks import _select_agent
from adcp.signing.keygen import generate_signing_keypair
from referencing import Registry, Resource
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from core.middleware.well_known import WellKnownTrustRootMiddleware
from src.core.trust_root import (
    agent_url_for_origin,
    brand_json_url,
    build_adagents_json,
    build_brand_json,
    build_jwks,
)

_SCHEMAS = Path(adcp.__file__).parent / "_schemas" / "3.2"
AGENT_URL = "https://azerion-gaming.sales-agent.yieldpro.improvedigital.com/mcp"
HOST = "azerion-gaming.sales-agent.yieldpro.improvedigital.com"
UPDATED = datetime(2026, 11, 4, tzinfo=UTC)


def _local_schema(uri: str) -> Resource:
    """Resolve ``https://adcontextprotocol.org/schemas/<ver>/<path>`` from the bundled copy (no network)."""
    rel = uri.split("/schemas/", 1)[1].split("/", 1)[1]
    return Resource.from_contents(json.loads((_SCHEMAS / rel).read_text()))


def _schema_errors(name: str, document: dict) -> list[str]:
    schema = json.loads((_SCHEMAS / name).read_text())
    validator = jsonschema.Draft7Validator(schema, registry=Registry(retrieve=_local_schema))
    return [e.message for e in validator.iter_errors(document)]


def _tenant(**overrides) -> SimpleNamespace:
    fields = {
        "name": "Azerion Gaming",
        "subdomain": "azerion-gaming",
        "updated_at": UPDATED,
        "brand_json_url": None,
        "public_agent_url": AGENT_URL,
        "is_embedded": False,
        "virtual_host": None,
    }
    return SimpleNamespace(**{**fields, **overrides})


def _key(created_at: datetime = UPDATED) -> SimpleNamespace:
    _pem, jwk = generate_signing_keypair(alg="ed25519", purpose="webhook-signing")
    return SimpleNamespace(public_jwk=jwk, created_at=created_at, rotated_out_at=None)


class TestBrandJson:
    def test_top_level_agents_is_schema_valid_and_resolvable(self):
        doc = build_brand_json(_tenant(), AGENT_URL, [])

        assert _schema_errors("brand.json", doc) == []
        assert "house" not in doc and "brands" not in doc
        assert [agent["url"] for agent in doc["agents"]] == [AGENT_URL]  # registry: jq '.agents[].url'
        picked = _select_agent(
            doc, f"https://{HOST}/.well-known/brand.json", agent_type="sales", agent_id=None, brand_id=None
        )
        assert picked.url == AGENT_URL
        assert picked.jwks_uri == f"https://{HOST}/.well-known/jwks.json"

    def test_agent_url_is_served_byte_exact(self):
        """A bare-origin agent URL must not gain a trailing slash (buyers byte-compare it)."""
        origin = f"https://{HOST}"
        doc = build_brand_json(_tenant(public_agent_url=origin), origin, [])

        assert doc["agents"][0]["url"] == origin
        assert doc["agents"][0]["id"] == "azerion_gaming_sales"

    def test_last_updated_tracks_newest_key(self):
        newer = UPDATED + timedelta(days=1)
        doc = build_brand_json(_tenant(), AGENT_URL, [_key(created_at=newer)])

        assert doc["last_updated"] == "2026-11-05T00:00:00Z"


class TestAgentUrlForOrigin:
    def test_always_the_mcp_endpoint_on_the_serving_host(self):
        """Regression: tenant settings (bare origin, other host) leaked into agents[].url instead of <host>/mcp."""
        assert agent_url_for_origin(f"https://{HOST}") == AGENT_URL


class TestBrandJsonUrl:
    def test_derived_from_agent_origin(self):
        assert brand_json_url(_tenant()) == f"https://{HOST}/.well-known/brand.json"

    def test_explicit_column_wins(self):
        explicit = "https://improvedigital.com/.well-known/brand.json"
        assert brand_json_url(_tenant(brand_json_url=explicit)) == explicit

    def test_none_without_agent_url(self):
        assert brand_json_url(_tenant(public_agent_url=None, is_embedded=True)) is None


class TestJwksAndAdagents:
    def test_jwks_publishes_stored_jwk_verbatim(self):
        key = _key()
        assert build_jwks([key]) == {"keys": [key.public_jwk]}

    def test_adagents_not_served_without_backing_property(self):
        """Never self-attests: no authorized property on this host means no document (404)."""
        assert build_adagents_json(_tenant(), AGENT_URL, [_key()], []) is None

    def test_adagents_inlines_host_properties_and_signing_keys(self):
        key = _key()
        prop = SimpleNamespace(
            property_id="games_site",
            property_type="website",
            name="Games",
            identifiers=[{"type": "domain", "value": HOST}],
            publisher_domain=HOST,
            tags=None,
        )
        doc = build_adagents_json(_tenant(), AGENT_URL, [key], [prop])

        assert _schema_errors("adagents.json", doc) == []
        (entry,) = doc["authorized_agents"]
        assert entry["url"] == f"https://{HOST}"
        assert entry["properties"][0]["property_id"] == "games_site"
        assert entry["signing_keys"] == [key.public_jwk]


class TestPublishableKeys:
    def test_keeps_active_and_recently_rotated_keys(self):
        from src.core.database.repositories.tenant_signing_credentials import TenantSigningCredentialRepository

        now = UPDATED
        active = SimpleNamespace(is_active=True, rotated_out_at=None)
        recent = SimpleNamespace(is_active=False, rotated_out_at=now - timedelta(hours=1))
        stale = SimpleNamespace(is_active=False, rotated_out_at=now - timedelta(days=2))
        repo = TenantSigningCredentialRepository(session=None, tenant_id="t1")  # type: ignore[arg-type]

        with patch.object(repo, "list_for_purpose", return_value=[active, recent, stale]):
            rows = repo.list_publishable("webhook-signing", now=now, grace_seconds=86_400)

        assert rows == [active, recent]


def _client(tenants: dict[str, str]) -> TestClient:
    async def resolve(host: str):
        tenant_id = tenants.get(host)
        return SimpleNamespace(id=tenant_id) if tenant_id else None

    inner = Starlette(routes=[Route("/{path:path}", lambda r: PlainTextResponse("inner"), methods=["GET", "POST"])])
    return TestClient(WellKnownTrustRootMiddleware(inner, resolve_tenant=resolve))


class TestMiddleware:
    def test_serves_document_for_known_host(self):
        client = _client({HOST: "t1"})
        with patch("core.middleware.well_known.build_trust_root_document", return_value={"keys": []}) as build:
            resp = client.get("/.well-known/jwks.json", headers={"host": f"{HOST}:443"})

        assert resp.status_code == 200
        assert resp.json() == {"keys": []}
        assert resp.headers["cache-control"] == "public, max-age=300"
        build.assert_called_once_with("t1", "/.well-known/jwks.json", HOST, f"http://{HOST}:443")

    def test_unknown_host_is_404(self):
        resp = _client({}).get("/.well-known/brand.json", headers={"host": "unknown.example.org"})
        assert resp.status_code == 404

    def test_unbuildable_document_is_404(self):
        client = _client({HOST: "t1"})
        with patch("core.middleware.well_known.build_trust_root_document", return_value=None):
            resp = client.get("/.well-known/brand.json", headers={"host": HOST})
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        ("method", "path"),
        [("GET", "/.well-known/agent-card.json"), ("GET", "/mcp"), ("POST", "/.well-known/brand.json")],
    )
    def test_other_requests_pass_through(self, method, path):
        resp = _client({HOST: "t1"}).request(method, path, headers={"host": HOST})
        assert resp.text == "inner"


def test_capabilities_advertise_brand_json_url():
    from core.platforms._capabilities_envelope import capabilities_for_request

    url = f"https://{HOST}/.well-known/brand.json"
    with (
        patch("core.platforms._capabilities_envelope._publisher_domains_for_tenant_id", return_value=[]),
        patch("core.platforms._capabilities_envelope._brand_json_url_for_tenant_id", return_value=url),
    ):
        scoped = capabilities_for_request(DecisioningCapabilities(), context=SimpleNamespace(tenant_id="t1"))

    assert scoped is not None
    assert scoped.identity is not None
    assert str(scoped.identity.brand_json_url) == url
