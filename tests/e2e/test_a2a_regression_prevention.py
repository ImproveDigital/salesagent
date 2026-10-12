#!/usr/bin/env python3
"""
A2A Regression Prevention Tests

These tests specifically target the bugs that slipped through our test coverage:
1. Agent card URLs that redirect (trailing-slash / path-suffix mistakes) and
   break client auth on the redirected request
2. Skills that are advertised but not actually dispatched into the core tools

Sales Agent 2.0 builds the card and the JSON-RPC handler inside the adcp
SDK (``core/main.py`` → ``adcp.decisioning.serve``), so there is no
``create_agent_card`` / ``AdCPRequestHandler`` to introspect any more. The
same regressions are caught here as observable HTTP contracts against the
live stack: the card URL is the host root and follows the request ``Host``,
the card endpoints never redirect, bearer auth is not path-gated, and every
advertised core skill reaches its tool.
"""

import logging
import uuid
from typing import Any
from urllib.parse import urlparse

import pytest
import requests

logger = logging.getLogger(__name__)

TENANT = "ci-test"
AGENT_CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")
CORE_SKILLS = ("get_products", "create_media_buy", "sync_creatives", "list_creatives")

# Hosts the multi-tenant public-URL resolver must reflect into the card
# (``SALES_AGENT_DOMAIN`` is ``sales-agent.example.com`` in the e2e stack).
TENANT_HOSTS = ("ci-test.sales-agent.example.com", "default.sales-agent.example.com")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skill_message(skill: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "contextId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"kind": "data", "data": {"skill": skill, "parameters": parameters}}],
            }
        },
    }


def _post(url: str, body: dict[str, Any], token: str | None) -> requests.Response:
    headers = {"Content-Type": "application/json", "x-adcp-tenant": TENANT}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return requests.post(url, json=body, headers=headers, timeout=30, allow_redirects=False)


def _parts(result: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [
        part
        for artifact in result.get("artifacts") or []
        for part in artifact.get("parts") or []
        if part.get("kind") == kind
    ]


def _dispatched_task(response: requests.Response, skill: str) -> dict[str, Any]:
    """Return the task result, asserting ``skill`` reached its tool.

    Completed tasks prove dispatch directly; failed tasks must carry the
    tool's ``adcp_error`` envelope (a request the *tool* rejected). The SDK's
    ``Unknown skill: ...`` text artifact means the skill was never dispatched.
    """
    assert response.status_code == 200, f"{skill}: HTTP {response.status_code}: {response.text[:300]}"
    body = response.json()
    assert "error" not in body, f"{skill}: JSON-RPC error: {body['error']}"
    result = body["result"]
    assert result.get("kind") == "task", f"{skill}: expected a task, got {result}"
    texts = [part.get("text", "") for part in _parts(result, "text")]
    assert not any(text.startswith("Unknown skill") for text in texts), f"{skill} not dispatched: {texts}"
    if result["status"]["state"] == "failed":
        data = [part["data"] for part in _parts(result, "data")]
        assert data and "adcp_error" in data[0], f"{skill}: failed without an adcp_error: {texts}"
    return result


@pytest.fixture
def a2a_base(live_server) -> str:
    return live_server["a2a"].rstrip("/")


@pytest.fixture
def agent_card(a2a_base) -> dict[str, Any]:
    response = requests.get(f"{a2a_base}/.well-known/agent.json", timeout=5)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAgentCardURLRegression:
    """Tests to prevent agent card URL issues that cause redirect/auth problems."""

    def test_agent_card_url_is_host_root(self, a2a_base, agent_card):
        """The card advertises the origin's root — a URL that never needs a redirect."""
        url = agent_card["url"]

        # Should be a valid absolute URL
        assert url.startswith(("http://", "https://")), f"Invalid URL format: {url}"
        parsed = urlparse(url)
        assert parsed.netloc, f"Agent card URL has no host: {url}"

        # 2.0 serves A2A at the host root: the path is exactly "/", nothing
        # more (a "/a2a" suffix or a missing slash would 3xx on the SDK router).
        assert parsed.path == "/", f"Agent card URL path must be '/': {url}"
        assert url == f"{a2a_base}/", f"Agent card URL must match the origin it was fetched from: {url}"

        # Every advertised interface points at the same URL
        interfaces = agent_card.get("supportedInterfaces") or []
        assert interfaces, "Agent card must advertise at least one interface"
        assert {interface["url"] for interface in interfaces} == {url}

    def test_dynamic_agent_card_urls_follow_request_host(self, a2a_base):
        """The per-request public URL resolver reflects the tenant host into the card.

        Multi-tenant deployments serve one card per subdomain; the URL must be
        rebuilt from the request ``Host`` (https for non-loopback hosts) and
        keep the bare-root path.
        """
        for host in TENANT_HOSTS:
            response = requests.get(f"{a2a_base}/.well-known/agent.json", headers={"Host": host}, timeout=5)
            assert response.status_code == 200, f"{host}: {response.text[:300]}"
            url = response.json()["url"]
            parsed = urlparse(url)
            assert parsed.scheme == "https", f"Non-loopback tenant host must advertise https: {url}"
            assert parsed.netloc == host, f"Card URL must follow the request Host {host}: {url}"
            assert parsed.path == "/", f"Generated URL must be the host root: {url}"

    def test_card_endpoints_advertise_one_consistent_url(self, a2a_base):
        """Both card endpoints (A2A 1.0 and the 0.3 alias) publish the same URL."""
        urls = set()
        for path in AGENT_CARD_PATHS:
            response = requests.get(f"{a2a_base}{path}", timeout=5)
            assert response.status_code == 200, f"{path}: {response.text[:300]}"
            card = response.json()
            urls.add(card["url"])
            urls.update(interface["url"] for interface in card.get("supportedInterfaces") or [])
        assert urls == {f"{a2a_base}/"}, f"Card endpoints disagree on the messaging URL: {urls}"

    @pytest.mark.integration
    def test_agent_card_http_endpoint_url_format(self, a2a_base):
        """Integration test: Verify actual HTTP endpoint returns correct URL format."""
        response = requests.get(f"{a2a_base}/.well-known/agent.json", timeout=5)
        assert response.status_code == 200
        url = response.json().get("url")

        assert url, "HTTP endpoint returned a card without url"
        assert url.endswith("/"), f"HTTP endpoint URL must be the bare host root: {url}"
        assert not url.endswith("/a2a") and "/a2a/" not in url, f"HTTP endpoint URL carries legacy /a2a path: {url}"


class TestSkillDispatchRegression:
    """Tests to prevent advertised skills from failing to reach the core tools."""

    def test_core_skills_are_dispatchable(self, agent_card, test_auth_token):
        """Every core skill advertised on the card is dispatched into its tool."""
        advertised = {skill["id"] for skill in agent_card["skills"]}
        assert set(CORE_SKILLS) <= advertised, f"Card missing core skills: {set(CORE_SKILLS) - advertised}"

        # Mutating skills get incomplete requests so the tool rejects them
        # (adcp_error) without creating state — still proof of dispatch.
        parameters = {
            "get_products": {"brief": "display advertising", "brand": {"domain": "testbrand.com"}},
            "list_creatives": {},
            "sync_creatives": {"creatives": []},
            "create_media_buy": {},
        }
        for skill in CORE_SKILLS:
            response = _post(agent_card["url"], _skill_message(skill, parameters[skill]), test_auth_token)
            _dispatched_task(response, skill)

    def test_read_skills_return_adcp_payloads(self, agent_card, test_auth_token):
        """Read-only skills complete and publish their AdCP payload as a DataPart."""
        expectations = {
            "get_products": (
                {"brief": "display advertising", "brand": {"domain": "testbrand.com"}},
                "products",
            ),
            "list_creatives": ({}, "creatives"),
        }
        for skill, (parameters, collection) in expectations.items():
            response = _post(agent_card["url"], _skill_message(skill, parameters), test_auth_token)
            result = _dispatched_task(response, skill)
            assert result["status"]["state"] == "completed", f"{skill} did not complete: {result['status']}"
            data = [part["data"] for part in _parts(result, "data")]
            assert data, f"{skill} completed without a DataPart"
            assert isinstance(data[0].get(collection), list), f"{skill} payload lacks {collection}: {data[0]}"

    def test_unknown_skill_is_rejected_cleanly(self, agent_card, test_auth_token):
        """An unknown skill fails the task explicitly instead of crashing the handler."""
        response = _post(agent_card["url"], _skill_message("no_such_skill", {}), test_auth_token)
        assert response.status_code == 200, response.text
        body = response.json()
        if "error" in body:
            assert body["error"]["code"] != -32603 or "no_such_skill" in body["error"]["message"]
        else:
            result = body["result"]
            assert result["status"]["state"] == "failed"
            texts = [part.get("text", "") for part in _parts(result, "text")]
            assert any("no_such_skill" in text for text in texts), texts


class TestAuthenticationFlow:
    """Tests to prevent authentication-related regressions."""

    def test_bearer_token_is_required(self, agent_card):
        """Requests without a bearer token never reach a skill."""
        response = _post(agent_card["url"], _skill_message("get_adcp_capabilities", {}), token=None)
        assert response.status_code == 401

    def test_invalid_bearer_token_is_rejected(self, agent_card):
        """A present-but-invalid token is rejected (not treated as anonymous)."""
        response = _post(agent_card["url"], _skill_message("get_adcp_capabilities", {}), token="not-a-real-token")
        assert response.status_code == 401

    def test_identity_resolution_scopes_skill_to_tenant(self, agent_card, test_auth_token):
        """A valid token resolves to a principal whose tenant catalog the skill reads."""
        response = _post(
            agent_card["url"],
            _skill_message("get_products", {"brief": "display advertising", "brand": {"domain": "testbrand.com"}}),
            test_auth_token,
        )
        result = _dispatched_task(response, "get_products")
        assert result["status"]["state"] == "completed"
        products = [part["data"] for part in _parts(result, "data")][0]["products"]
        assert products, "Authenticated principal must see its tenant's products"


class TestHTTPBehaviorRegression:
    """Tests to prevent HTTP-level bugs like redirect issues."""

    def test_auth_applies_to_every_protocol_path(self, a2a_base):
        """Bearer auth is not path-gated: any non-discovery POST without a token is 401.

        Previously the A2A auth middleware only ran for ``/a2a`` paths, so a
        client hitting the root (or a stale ``/a2a`` URL) bypassed it.
        """
        body = {"jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {}}
        for path in ("/", "/a2a", "/a2a/"):
            response = requests.post(f"{a2a_base}{path}", json=body, timeout=5, allow_redirects=False)
            assert response.status_code == 401, f"POST {path} without a token must be 401, got {response.status_code}"

    def test_discovery_paths_stay_public(self, a2a_base):
        """Agent-card discovery must work before the buyer has credentials."""
        for path in AGENT_CARD_PATHS:
            response = requests.get(f"{a2a_base}{path}", timeout=5)
            assert response.status_code == 200, f"{path} must be reachable without auth"

    @pytest.mark.integration
    def test_no_redirect_on_agent_card_endpoints(self, a2a_base, test_auth_token):
        """Integration test: Verify agent card endpoints (and the card URL) don't redirect."""
        for endpoint in AGENT_CARD_PATHS:
            # Use allow_redirects=False to catch any redirects
            response = requests.get(f"{a2a_base}{endpoint}", allow_redirects=False, timeout=5)

            # Should be 200, not a redirect (301, 302, etc.)
            assert response.status_code == 200, f"Endpoint {endpoint} returned {response.status_code}"

            # Should return JSON
            assert response.headers.get("content-type", "").startswith("application/json")

            # Should have agent card data
            data = response.json()
            assert "name" in data
            assert "url" in data

            # The advertised URL itself must not redirect (a redirect drops
            # the Authorization header on most clients).
            response = _post(data["url"], _skill_message("get_adcp_capabilities", {}), test_auth_token)
            assert response.status_code == 200, f"Card URL {data['url']} redirected/failed: {response.status_code}"


# Summary test to run all regression checks
def test_regression_prevention_summary(a2a_base, test_auth_token):
    """Summary test that runs key regression checks."""
    # 1. Agent card URL format
    agent_card = requests.get(f"{a2a_base}/.well-known/agent.json", timeout=5).json()
    assert agent_card["url"] == f"{a2a_base}/", "REGRESSION: Agent card URL is not the host root"

    # 2. Core skill dispatches into its tool
    response = _post(
        agent_card["url"],
        _skill_message("get_products", {"brief": "display advertising", "brand": {"domain": "testbrand.com"}}),
        test_auth_token,
    )
    result = _dispatched_task(response, "get_products")
    assert result["status"]["state"] == "completed", "REGRESSION: get_products not dispatched"

    # 3. Handler enforces bearer auth
    assert _post(agent_card["url"], _skill_message("get_products", {}), token=None).status_code == 401, (
        "REGRESSION: unauthenticated request reached the handler"
    )

    logger.info("✅ All regression prevention checks passed")
