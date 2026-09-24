#!/usr/bin/env python3
"""
A2A Standard Endpoints Test - live-stack version.

Sales Agent 2.0 serves the A2A surface through the adcp SDK's ``serve(...)``
(see ``core/main.py``): the host root ``/`` *is* the JSON-RPC messaging
endpoint, and the agent card is built by the SDK from the AdCP tool
definitions and published at ``/.well-known/agent-card.json`` (A2A 1.0) and
``/.well-known/agent.json`` (0.3 alias). The hand-written
``src.a2a_server.adcp_a2a_server`` module (``create_agent_card``,
``AdCPRequestHandler``) no longer exists, so every contract that used to be
asserted on those Python objects is asserted here against the running stack
over HTTP: the card exists and parses with the a2a-sdk, it advertises the
AdCP skills, the JSON-RPC handler dispatches those skills, and bearer auth
gates the messaging endpoint.
"""

import json
import os
import uuid
from typing import Any

import pytest
import requests
from adcp import get_adcp_spec_version
from adcp.validation.version import resolve_bundle_key

# ``name`` passed to ``serve(...)`` in ``core.main._serve_kwargs``.
EXPECTED_AGENT_NAME = "salesagent-core"

# A2A 1.0 canonical card path and the 0.3 alias the SDK serves alongside it.
AGENT_CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")

# Core AdCP skills every sales agent must publish and dispatch.
# Note: get_signals is optional (signals may come from dedicated agents).
EXPECTED_SKILLS = {"get_products", "create_media_buy", "sync_creatives", "list_creatives"}

TENANT = "ci-test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jsonrpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}


def _skill_message(skill: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Explicit-skill ``message/send`` body (DataPart ``{skill, parameters}``)."""
    return _jsonrpc(
        "message/send",
        {
            "message": {
                "messageId": str(uuid.uuid4()),
                "contextId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"kind": "data", "data": {"skill": skill, "parameters": parameters}}],
            }
        },
    )


def _post(url: str, body: dict[str, Any], token: str | None, tenant: str | None = TENANT) -> requests.Response:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if tenant:
        headers["x-adcp-tenant"] = tenant
    return requests.post(url, json=body, headers=headers, timeout=30)


def _data_parts(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        part["data"]
        for artifact in result.get("artifacts") or []
        for part in artifact.get("parts") or []
        if part.get("kind") == "data" and isinstance(part.get("data"), dict)
    ]


def _text_parts(result: dict[str, Any]) -> list[str]:
    return [
        part.get("text", "")
        for artifact in result.get("artifacts") or []
        for part in artifact.get("parts") or []
        if part.get("kind") == "text"
    ]


def _assert_skill_dispatched(response: requests.Response, skill: str) -> dict[str, Any]:
    """Assert the JSON-RPC response shows ``skill`` reached the business logic.

    A dispatched skill yields a task that either completed or failed with an
    ``adcp_error`` envelope (request rejected *by the tool*). An undispatched
    skill surfaces as the SDK's ``Unknown skill: ...`` text artifact.
    Returns the task ``result``.
    """
    assert response.status_code == 200, f"{skill}: HTTP {response.status_code}: {response.text[:300]}"
    body = response.json()
    assert "error" not in body, f"{skill}: JSON-RPC error: {body['error']}"
    result = body["result"]
    assert result.get("kind") == "task", f"{skill}: expected a task result, got {json.dumps(result)[:300]}"
    texts = _text_parts(result)
    assert not any(text.startswith("Unknown skill") for text in texts), f"{skill} was not dispatched: {texts}"
    state = (result.get("status") or {}).get("state")
    if state == "failed":
        data = _data_parts(result)
        assert data and "adcp_error" in data[0], (
            f"{skill}: failed task must carry an adcp_error from the tool; got {json.dumps(result)[:500]}"
        )
    return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def a2a_base(live_server) -> str:
    """Base URL of the unified server (A2A lives at the host root)."""
    return live_server["a2a"].rstrip("/")


@pytest.fixture
def agent_card(a2a_base) -> dict[str, Any]:
    """The live agent card from the 0.3 alias path."""
    response = requests.get(f"{a2a_base}/.well-known/agent.json", timeout=5)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestA2AEndpointsActual:
    """Test actual A2A endpoints that we implement."""

    @pytest.mark.integration
    def test_well_known_agent_json_endpoint_live(self, a2a_base):
        """Test /.well-known/agent.json endpoint against live server."""
        response = requests.get(f"{a2a_base}/.well-known/agent.json", timeout=5)

        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/json")

        data = response.json()
        assert "name" in data
        assert "description" in data
        assert "version" in data
        assert "skills" in data
        assert "url" in data

        # 2.0 contract: the host root IS the A2A surface, so the advertised
        # messaging URL is the origin with a bare "/" path — no "/a2a" suffix
        # (which would redirect) and nothing after the slash.
        url = data["url"]
        assert url == f"{a2a_base}/", f"Agent card URL must be the host root: {url}"
        assert not url.endswith("/a2a") and "/a2a/" not in url, f"Legacy /a2a layout leaked into card URL: {url}"

        assert data["name"] == EXPECTED_AGENT_NAME

        # Should have skills
        assert isinstance(data["skills"], list)
        assert len(data["skills"]) > 0

        # Should specify security configuration (A2A spec for authentication)
        assert "security" in data or "securitySchemes" in data

        # AdCP advertisement: every skill is an AdCP task and the AdCP
        # capability skill is published for buyers to probe versions.
        skill_ids = {skill["id"] for skill in data["skills"]}
        assert "get_adcp_capabilities" in skill_ids
        assert all("adcp" in (skill.get("tags") or []) for skill in data["skills"])

    @pytest.mark.integration
    def test_agent_card_json_endpoint_live(self, a2a_base, agent_card):
        """Test the A2A 1.0 /.well-known/agent-card.json endpoint against live server.

        (Replaces the legacy ``/agent.json`` alias, which 2.0 does not serve.)
        """
        response = requests.get(f"{a2a_base}/.well-known/agent-card.json", timeout=5)

        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/json")
        data = response.json()
        assert data["name"] == EXPECTED_AGENT_NAME

        # Same URL contract as the 0.3 alias — one card, two paths.
        assert data["url"] == f"{a2a_base}/"
        assert data == agent_card

    @pytest.mark.integration
    def test_a2a_endpoint_accessible(self, a2a_base, agent_card):
        """Test that the advertised messaging endpoint is accessible (may require auth)."""
        # Host root, with and without the trailing slash the card advertises.
        for url in (agent_card["url"], a2a_base):
            response = requests.post(url, json={"test": "data"}, timeout=5)

            # Should not be 404 (endpoint exists) — unauthenticated requests get 401
            assert response.status_code != 404, f"Endpoint {url} should exist"
            assert response.status_code == 401, f"Unauthenticated POST to {url} should be rejected with 401"

    @pytest.mark.integration
    def test_cors_headers_present(self, a2a_base):
        """Test that CORS headers are present for browser compatibility."""
        # CORS headers are only returned when the Origin matches an allowed origin.
        # Default ALLOWED_ORIGINS is "http://localhost:8000" — use that as Origin.
        allowed_origin = os.getenv("ALLOWED_ORIGINS", "http://localhost:8000").split(",")[0].strip()

        response = requests.get(
            f"{a2a_base}/.well-known/agent.json",
            headers={"Origin": allowed_origin},
            timeout=5,
        )

        assert response.status_code == 200
        # Should have CORS headers for an allowed origin
        assert "Access-Control-Allow-Origin" in response.headers, "Missing CORS headers"
        assert response.headers["Access-Control-Allow-Origin"] == allowed_origin

    @pytest.mark.integration
    def test_options_preflight_support(self, a2a_base):
        """Test that CORS preflight OPTIONS requests are answered for the discovery and messaging endpoints."""
        allowed_origin = os.getenv("ALLOWED_ORIGINS", "http://localhost:8000").split(",")[0].strip()

        for path, method in (("/.well-known/agent.json", "GET"), ("/", "POST")):
            response = requests.options(
                f"{a2a_base}{path}",
                headers={
                    "Origin": allowed_origin,
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
                timeout=5,
            )

            # Should handle OPTIONS preflight requests
            assert response.status_code in [200, 204], f"OPTIONS preflight for {path} should be handled"
            assert response.headers.get("Access-Control-Allow-Origin") == allowed_origin
            assert method in response.headers.get("Access-Control-Allow-Methods", "")


class TestA2AAgentCardContract:
    """Test the SDK-built agent card the live server publishes."""

    def test_agent_card_structure(self, a2a_base, agent_card):
        """The published card carries every field an A2A client needs."""
        for field in ("name", "description", "version", "skills", "url"):
            assert field in agent_card, f"Agent card missing {field}"

        # Check for security configuration (A2A spec compliant way to specify authentication)
        assert "security" in agent_card or "securitySchemes" in agent_card

        # Validate content
        assert agent_card["name"] == EXPECTED_AGENT_NAME

        # Critical: the URL is the host root the card was fetched from.
        assert agent_card["url"] == f"{a2a_base}/", f"Agent card URL must be the host root: {agent_card['url']}"

        # Should have skills
        assert len(agent_card["skills"]) > 0

        # Validate skills structure
        for skill in agent_card["skills"]:
            assert skill.get("id")
            assert skill.get("name")
            assert skill.get("description")

    def test_agent_card_advertises_adcp(self, agent_card, live_server, test_auth_token):
        """The card advertises AdCP support and the negotiated AdCP version.

        The pre-2.0 card carried a hand-rolled ``adcp-extension`` in
        ``capabilities.extensions``; the SDK card instead tags every skill
        ``adcp`` and publishes ``get_adcp_capabilities``, whose payload is the
        authoritative version / protocol advertisement.
        """
        skill_ids = {skill["id"] for skill in agent_card["skills"]}
        assert "get_adcp_capabilities" in skill_ids, "AdCP capability skill not found in live agent card"
        for skill in agent_card["skills"]:
            assert "adcp" in (skill.get("tags") or []), f"Skill {skill['id']} is not tagged as an AdCP task"

        response = _post(agent_card["url"], _skill_message("get_adcp_capabilities", {}), test_auth_token)
        result = _assert_skill_dispatched(response, "get_adcp_capabilities")
        assert result["status"]["state"] == "completed"
        capabilities = _data_parts(result)[0]

        # Validate AdCP capability values
        expected_release = resolve_bundle_key(get_adcp_spec_version())
        supported_versions = capabilities["adcp"]["supported_versions"]
        assert isinstance(supported_versions, list)
        assert expected_release in supported_versions, (
            f"Server does not advertise AdCP {expected_release}: {supported_versions}"
        )
        protocols = capabilities["supported_protocols"]
        assert isinstance(protocols, list)
        assert len(protocols) >= 1
        assert "media_buy" in protocols

    def test_agent_card_skills_coverage(self, agent_card):
        """Test that agent card includes expected AdCP skills."""
        skill_names = {skill["name"] for skill in agent_card["skills"]}
        skill_ids = {skill["id"] for skill in agent_card["skills"]}

        for expected_skill in EXPECTED_SKILLS:
            assert expected_skill in skill_ids, f"Missing expected skill: {expected_skill}"
            assert expected_skill in skill_names, f"Missing expected skill name: {expected_skill}"

    def test_agent_card_serialization(self, agent_card):
        """Test that the published card round-trips through JSON and the a2a-sdk models."""
        # Should be JSON serializable and parse back unchanged
        json_str = json.dumps(agent_card)
        assert len(json_str) > 0
        parsed = json.loads(json_str)
        assert parsed["name"] == EXPECTED_AGENT_NAME

        # A2A 0.3 clients (a2a-sdk compat layer) must be able to load the card
        from a2a.compat.v0_3.types import AgentCard as CompatAgentCard

        compat_card = CompatAgentCard.model_validate(agent_card)
        assert compat_card.name == EXPECTED_AGENT_NAME
        assert compat_card.url == agent_card["url"]
        assert {skill.id for skill in compat_card.skills} >= EXPECTED_SKILLS

        # A2A 1.0 clients (protobuf card) must be able to load it too
        from a2a import types as pb
        from google.protobuf.json_format import ParseDict

        proto_card = ParseDict(agent_card, pb.AgentCard(), ignore_unknown_fields=True)
        assert proto_card.name == EXPECTED_AGENT_NAME
        assert {interface.url for interface in proto_card.supported_interfaces} == {agent_card["url"]}


class TestA2ARequestHandling:
    """Test the A2A JSON-RPC request handler over the wire."""

    def test_handler_initialization(self, agent_card, test_auth_token):
        """The JSON-RPC handler answers at the advertised URL with a task store."""
        response = _post(agent_card["url"], _jsonrpc("tasks/get", {"id": "does-not-exist"}), test_auth_token)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["jsonrpc"] == "2.0"
        # Unknown task → JSON-RPC error from the task store, not an HTTP error
        assert "error" in body
        assert "not found" in body["error"]["message"].lower()

    def test_handler_has_required_methods(self, agent_card, test_auth_token):
        """The handler dispatches every required A2A method."""
        method_not_found = -32601
        requests_by_method = {
            "message/send": _skill_message("get_adcp_capabilities", {}),
            "tasks/get": _jsonrpc("tasks/get", {"id": "does-not-exist"}),
            "tasks/cancel": _jsonrpc("tasks/cancel", {"id": "does-not-exist"}),
        }

        for method, body in requests_by_method.items():
            response = _post(agent_card["url"], body, test_auth_token)
            assert response.status_code == 200, f"{method}: HTTP {response.status_code}: {response.text[:300]}"
            payload = response.json()
            assert payload["id"] == body["id"], f"{method}: JSON-RPC id must echo the request id"
            if "error" in payload:
                assert payload["error"]["code"] != method_not_found, f"Handler missing method: {method}"
            else:
                assert "result" in payload, f"{method}: neither result nor error in {payload}"

    def test_handler_has_skill_methods(self, agent_card, test_auth_token):
        """The handler dispatches each core AdCP skill into its tool."""
        # Read skills complete; mutating skills are sent with a deliberately
        # incomplete request so the tool rejects them with an adcp_error —
        # proof of dispatch without creating state.
        skill_requests = {
            "get_products": {"brief": "display advertising", "brand": {"domain": "testbrand.com"}},
            "list_creatives": {},
            "sync_creatives": {"creatives": []},
            "create_media_buy": {},
        }

        for skill, parameters in skill_requests.items():
            response = _post(agent_card["url"], _skill_message(skill, parameters), test_auth_token)
            _assert_skill_dispatched(response, skill)

    def test_auth_methods_exist(self, agent_card, test_auth_token):
        """Bearer-token identity resolution guards the messaging endpoint."""
        body = _skill_message("get_adcp_capabilities", {})

        # No token → rejected before dispatch
        assert _post(agent_card["url"], body, token=None).status_code == 401
        # Invalid token → rejected before dispatch
        assert _post(agent_card["url"], body, token="invalid-token").status_code == 401
        # Valid token → identity resolved, skill dispatched
        response = _post(agent_card["url"], body, test_auth_token)
        result = _assert_skill_dispatched(response, "get_adcp_capabilities")
        assert result["status"]["state"] == "completed"


class TestA2AServerIntegration:
    """Integration tests for complete A2A server setup."""

    @pytest.mark.integration
    def test_server_discovery_flow(self, a2a_base, test_auth_token):
        """Test complete A2A client discovery flow."""
        # Step 1: Client discovers agent
        response = requests.get(f"{a2a_base}/.well-known/agent.json", timeout=5)
        assert response.status_code == 200, "A2A server not responding"

        agent_card = response.json()

        # Step 2: Validate agent card has what client needs
        assert "skills" in agent_card
        assert "security" in agent_card or "securitySchemes" in agent_card  # A2A spec authentication
        assert "url" in agent_card

        # Step 3: Validate URL format for messaging — the host root, exactly as served
        messaging_url = agent_card["url"]
        assert messaging_url == f"{a2a_base}/", "URL must be the host root (2.0 serves A2A at '/')"

        # Step 4: The messaging endpoint exists (auth required, but never 404)
        response = requests.post(messaging_url, json={"test": "message"}, timeout=5)
        assert response.status_code != 404, "Messaging endpoint should exist"

        # Step 5: An authenticated client can invoke a skill at that URL
        response = _post(messaging_url, _skill_message("get_adcp_capabilities", {}), test_auth_token)
        result = _assert_skill_dispatched(response, "get_adcp_capabilities")
        assert result["status"]["state"] == "completed"

    @pytest.mark.integration
    def test_authentication_flow(self, a2a_base):
        """Test authentication requirements."""
        # Should require Bearer token for messaging
        response = requests.post(
            f"{a2a_base}/",
            headers={"Authorization": "Bearer invalid-token"},
            json={"method": "message/send", "params": {}},
            timeout=5,
        )

        # Should reject invalid token (401) not be 404
        assert response.status_code != 404, "Endpoint should exist"
        assert response.status_code == 401, "Invalid token must be rejected with 401"

        # Missing auth should also not be 404
        response = requests.post(f"{a2a_base}/", json={"method": "message/send", "params": {}}, timeout=5)
        assert response.status_code != 404, "Endpoint should exist even without auth"
        assert response.status_code == 401, "Missing token must be rejected with 401"


def test_a2a_regression_summary(a2a_base, test_auth_token):
    """Quick summary test for key regressions."""
    # Test 1: Agent card URL format — host root, served for the requested origin
    agent_card = requests.get(f"{a2a_base}/.well-known/agent.json", timeout=5).json()
    assert agent_card["url"] == f"{a2a_base}/", "REGRESSION: Agent card URL is not the host root"

    # Test 2: Handler answers JSON-RPC at the advertised URL
    response = _post(agent_card["url"], _jsonrpc("tasks/get", {"id": "does-not-exist"}), test_auth_token)
    assert response.status_code == 200 and "error" in response.json(), "REGRESSION: A2A handler unreachable"

    # Test 3: Core skill dispatches into the tool and returns AdCP data
    response = _post(
        agent_card["url"],
        _skill_message("get_products", {"brief": "display advertising", "brand": {"domain": "testbrand.com"}}),
        test_auth_token,
    )
    result = _assert_skill_dispatched(response, "get_products")
    assert result["status"]["state"] == "completed", "REGRESSION: get_products did not complete"
    assert isinstance(_data_parts(result)[0].get("products"), list), "REGRESSION: get_products returned no products"

    print("✅ A2A regression tests passed")
