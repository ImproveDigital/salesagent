"""End-to-end integration test for SigningVerifyMiddleware.

PR 2B follow-up of [signing-non-embedded](../../../docs/design/signing-non-embedded.md):
proves the middleware actually works with real signed HTTP requests against a
mocked brand.json + JWKS publication. Without this, PR 2B's unit tests cover
the path filter and contextvar plumbing, but never exercise the verifier
checklist itself.

What this test simulates:

1. Tenant + principal + admitted_operator + active operator_advertiser_link.
2. Operator publishes a brand.json at ``https://op.example.com/.well-known/brand.json``
   listing one ``buying`` agent with a ``jwks_uri``.
3. JWKS endpoint at that ``jwks_uri`` serves the public half of an Ed25519
   keypair we generate in-process.
4. We sign a real request using the matching private PEM and POST it through
   a Starlette test app that has SigningVerifyMiddleware mounted around a
   sentinel handler.
5. Assert: signed request → 200 + verified state attached. Bad signature →
   401 with the spec error code. Unsigned → passes through (PR 2B no-enforce).
6. Trusted operator (is_trusted=True) → middleware bypasses verification.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
import pytest
from adcp.signing import (
    BrandJsonJwksResolver,
    generate_signing_keypair,
    load_private_key_pem,
    sign_request,
)
from sqlalchemy.orm import Session as SASession
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.core.database.database_session import get_engine
from src.core.signing import (
    SigningVerifyMiddleware,
    clear_verified_state,
    get_operator_brand_json_cache,
    get_verified_state,
)
from src.core.signing.replay_store import reset_for_tests as reset_replay_store

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


# ---------------------------------------------------------------------------
# Mock brand.json + JWKS infrastructure
# ---------------------------------------------------------------------------


BRAND_JSON_URL = "https://op.example.com/.well-known/brand.json"
JWKS_URI = "https://op.example.com/.well-known/jwks.json"
AGENT_URL = "https://op.example.com/agents/buying"


def _build_brand_json(public_jwk: dict) -> dict:
    """Brand.json shape per AdCP spec: agents[] with one buying agent.

    Library's ``_pick_agent`` reads ``type``, ``url``, ``jwks_uri`` (and
    optional ``id``) — the field names are bare, not prefixed.
    """
    return {
        "version": "1.0",
        "brand": {"id": "op.example.com", "display_name": "Op Example"},
        "agents": [
            {
                "url": AGENT_URL,
                "type": "buying",
                "jwks_uri": JWKS_URI,
            }
        ],
    }


def _build_jwks(public_jwk: dict) -> dict:
    """RFC 7517 JWK Set."""
    return {"keys": [public_jwk]}


def _make_mock_client_factory(brand_json: dict, jwks: dict):
    """httpx mock factory that serves brand.json + JWKS for the resolver."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == BRAND_JSON_URL:
            return httpx.Response(
                200,
                json=brand_json,
                headers={"Cache-Control": "max-age=300", "Content-Type": "application/json"},
            )
        if url == JWKS_URI:
            return httpx.Response(
                200,
                json=jwks,
                headers={"Cache-Control": "max-age=300", "Content-Type": "application/json"},
            )
        return httpx.Response(404, text=f"unmocked: {url}")

    transport = httpx.MockTransport(handler)

    @asynccontextmanager
    async def factory(url: str):
        async with httpx.AsyncClient(transport=transport) as client:
            yield client

    return factory


# ---------------------------------------------------------------------------
# Fixtures: keypair + DB rows + middleware-wrapped Starlette app
# ---------------------------------------------------------------------------


async def _sentinel_handler(request: Request) -> JSONResponse:
    """The 'inner' handler our middleware wraps. Echoes back the verified
    state AND the body bytes it sees so tests can assert that the middleware
    correctly replays the body to downstream handlers (PR 2C body-buffering)."""
    body = await request.body()
    verified = get_verified_state()
    return JSONResponse(
        {
            "verified": verified is not None,
            "operator_id": verified.operator_id if verified else None,
            "agent_url": verified.agent_url if verified else None,
            "key_id": verified.key_id if verified else None,
            "body_seen": body.decode("utf-8") if body else "",
        }
    )


@pytest.fixture
def session(integration_db):
    from tests.factories import ALL_FACTORIES

    engine = get_engine()
    sess = SASession(bind=engine)
    try:
        for f in ALL_FACTORIES:
            f._meta.sqlalchemy_session = sess
        yield sess
    finally:
        sess.close()
        clear_verified_state()
        # Reset the brand.json cache singleton so cross-test resolvers don't bleed.
        get_operator_brand_json_cache().clear()
        reset_replay_store()


@pytest.fixture
def keypair():
    """Generate a fresh Ed25519 keypair per test."""
    pem, public_jwk = generate_signing_keypair(alg="ed25519", purpose="request-signing")
    return pem, public_jwk


@pytest.fixture
def signing_setup(session, keypair):
    """Provision tenant + principal + operator + link + cache resolver.

    Returns a dict carrying everything a signed-request test needs:
    ``tenant_id``, ``principal_id``, ``access_token``, ``operator_id``,
    ``private_key``, ``key_id``.
    """
    from tests.factories import (
        AdmittedOperatorFactory,
        OperatorAdvertiserLinkFactory,
        PrincipalFactory,
        TenantFactory,
    )

    pem, public_jwk = keypair
    key_id = public_jwk["kid"]

    # Bootstrap PgReplayStore on the test DB. The production startup hook
    # creates the adcp_replay table once at boot; integration_db creates a
    # fresh DB per test so we redo the schema bootstrap here.
    reset_replay_store()
    os.environ["REPLAY_SWEEP_MODE"] = "off"
    from src.core.signing import bootstrap_replay_store

    bootstrap_replay_store()

    tenant = TenantFactory(tenant_id="t_signing_e2e")
    operator = AdmittedOperatorFactory(
        tenant=tenant,
        operator_id="op_signing",
        brand_json_url=BRAND_JSON_URL,
        is_trusted=False,
        is_active=True,
    )
    principal = PrincipalFactory(
        tenant=tenant,
        principal_id="p_signing",
        access_token="signing-test-token",
        bound_operator_id=operator.operator_id,
    )
    OperatorAdvertiserLinkFactory(
        operator=operator,
        principal=principal,
        billing_mode="operator_bills",
        is_active=True,
    )
    session.commit()

    # Pre-populate the cache with a resolver wired to the mocked httpx
    # transport. This bypasses the real network for brand.json + JWKS fetches
    # while still exercising the production BrandJsonJwksResolver code path.
    # ``_client_factory`` mocks the brand.json hop; ``jwks_fetcher`` mocks the
    # inner JWKS hop (the brand.json resolver constructs an
    # ``AsyncCachingJwksResolver`` internally for the agent's jwks_uri).
    cache = get_operator_brand_json_cache()
    cache.clear()
    brand_json = _build_brand_json(public_jwk)
    jwks = _build_jwks(public_jwk)
    factory = _make_mock_client_factory(brand_json, jwks)

    async def fake_jwks_fetcher(uri: str, *, allow_private: bool = False) -> dict:
        if uri == JWKS_URI:
            return jwks
        raise RuntimeError(f"unexpected JWKS uri: {uri}")

    resolver = BrandJsonJwksResolver(
        BRAND_JSON_URL,
        agent_type="buying",
        allow_private_destinations=True,
        jwks_fetcher=fake_jwks_fetcher,
        _client_factory=factory,
    )
    cache._resolvers[(BRAND_JSON_URL, "buying")] = resolver

    private_key = load_private_key_pem(pem)

    return {
        "tenant_id": tenant.tenant_id,
        "principal_id": principal.principal_id,
        "access_token": principal.access_token,
        "operator_id": operator.operator_id,
        "private_key": private_key,
        "key_id": key_id,
    }


def _build_app() -> Starlette:
    """Starlette app: SigningVerifyMiddleware wrapped around _sentinel_handler.

    Mounts on /mcp/ to satisfy the buyer-protocol-path filter.
    """
    routes = [Route("/mcp/", _sentinel_handler, methods=["POST"])]
    app = Starlette(routes=routes)
    # Wrap manually rather than via Starlette's Middleware chain so we exercise
    # the same ASGI integration path core/main.py uses with serve().
    return SigningVerifyMiddleware(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSigningMiddlewareEndToEnd:
    """End-to-end coverage for the verifier middleware.

    Each test runs an in-process httpx ASGITransport against the wrapped app.
    The middleware does real DB lookups (principal → operator → policy) and
    real verifier work (cryptographic signature check + replay store insert)
    — only the brand.json + JWKS network fetches are mocked.
    """

    async def test_unsigned_request_passes_through(self, signing_setup):
        """No signature → middleware no-op → handler runs, no verified state."""
        app = _build_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.example.com") as client:
            response = await client.post(
                "/mcp/",
                headers={
                    "x-adcp-auth": signing_setup["access_token"],
                    "x-adcp-tenant": signing_setup["tenant_id"],
                },
                json={"hello": "world"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["verified"] is False

    async def test_signed_request_accepted_and_state_attached(self, signing_setup):
        """Valid signature → 200 + verified state on the handler's contextvar."""
        body = b'{"hello":"world"}'
        url = "http://t.example.com/mcp/"
        method = "POST"

        signed = sign_request(
            method=method,
            url=url,
            headers={
                "host": "t.example.com",
                "content-type": "application/json",
                "x-adcp-auth": signing_setup["access_token"],
                "x-adcp-tenant": signing_setup["tenant_id"],
            },
            body=body,
            private_key=signing_setup["private_key"],
            key_id=signing_setup["key_id"],
            alg="ed25519",
            cover_content_digest=False,
        )

        app = _build_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.example.com") as client:
            response = await client.post(
                "/mcp/",
                content=body,
                headers={
                    "x-adcp-auth": signing_setup["access_token"],
                    "x-adcp-tenant": signing_setup["tenant_id"],
                    "content-type": "application/json",
                    **signed.as_dict(),
                },
            )

        assert response.status_code == 200, f"body: {response.text!r}"
        result = response.json()
        assert result["verified"] is True
        assert result["operator_id"] == signing_setup["operator_id"]
        assert result["key_id"] == signing_setup["key_id"]

    async def test_bad_signature_returns_401_with_spec_code(self, signing_setup):
        """Tampered signature → 401 + ``WWW-Authenticate: Signature error="..."``."""
        body = b'{"hello":"world"}'
        url = "http://t.example.com/mcp/"

        signed = sign_request(
            method="POST",
            url=url,
            headers={
                "host": "t.example.com",
                "content-type": "application/json",
                "x-adcp-auth": signing_setup["access_token"],
                "x-adcp-tenant": signing_setup["tenant_id"],
            },
            body=body,
            private_key=signing_setup["private_key"],
            key_id=signing_setup["key_id"],
            alg="ed25519",
            cover_content_digest=False,
        )
        # Corrupt the signature payload. The header format is
        # ``sig=:<base64-bytes>:``; we flip several bytes inside the base64
        # segment so the recovered signature bytes differ from what the
        # signer computed.
        sig_header = signed.signature
        # Find the base64 segment between the colons.
        first_colon = sig_header.index(":")
        last_colon = sig_header.rindex(":")
        prefix = sig_header[: first_colon + 1]
        b64 = sig_header[first_colon + 1 : last_colon]
        suffix = sig_header[last_colon:]
        # Flip the first 8 base64 chars to ensure the decoded bytes change.
        flipped = "".join("A" if c != "A" else "B" for c in b64[:8]) + b64[8:]
        tampered_signature = prefix + flipped + suffix
        tampered_headers = signed.as_dict()
        tampered_headers["Signature"] = tampered_signature

        app = _build_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.example.com") as client:
            response = await client.post(
                "/mcp/",
                content=body,
                headers={
                    "x-adcp-auth": signing_setup["access_token"],
                    "x-adcp-tenant": signing_setup["tenant_id"],
                    "content-type": "application/json",
                    **tampered_headers,
                },
            )

        assert response.status_code == 401, f"body: {response.text!r}"
        www_auth = response.headers.get("www-authenticate", "")
        assert www_auth.startswith('Signature error="'), www_auth
        # Spec error codes start with "request_signature_"
        assert "request_signature_" in www_auth

    async def test_replay_rejected(self, signing_setup):
        """Same nonce twice in the window → second request 401 ``request_signature_replayed``.

        Exercises the PgReplayStore path. The first request is accepted; the
        second (same body, same Signature-Input headers) trips the replay check
        because the replay store remembers the nonce.
        """
        body = b'{"hello":"replay"}'
        url = "http://t.example.com/mcp/"
        signed = sign_request(
            method="POST",
            url=url,
            headers={
                "host": "t.example.com",
                "content-type": "application/json",
                "x-adcp-auth": signing_setup["access_token"],
                "x-adcp-tenant": signing_setup["tenant_id"],
            },
            body=body,
            private_key=signing_setup["private_key"],
            key_id=signing_setup["key_id"],
            alg="ed25519",
            cover_content_digest=False,
        )
        send_headers = {
            "x-adcp-auth": signing_setup["access_token"],
            "x-adcp-tenant": signing_setup["tenant_id"],
            "content-type": "application/json",
            **signed.as_dict(),
        }

        app = _build_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.example.com") as client:
            first = await client.post("/mcp/", content=body, headers=send_headers)
            second = await client.post("/mcp/", content=body, headers=send_headers)

        assert first.status_code == 200, f"first body: {first.text!r}"
        assert second.status_code == 401, f"second body: {second.text!r}"
        assert "request_signature_replayed" in second.headers.get("www-authenticate", "")

    async def test_body_replayed_to_downstream_handler(self, signing_setup):
        """PR 2C body-buffering: the verifier reads the body once, then the
        middleware re-emits the same bytes to the downstream handler."""
        body = b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_products","arguments":{}},"id":1}'
        url = "http://t.example.com/mcp/"

        signed = sign_request(
            method="POST",
            url=url,
            headers={
                "host": "t.example.com",
                "content-type": "application/json",
                "x-adcp-auth": signing_setup["access_token"],
                "x-adcp-tenant": signing_setup["tenant_id"],
            },
            body=body,
            private_key=signing_setup["private_key"],
            key_id=signing_setup["key_id"],
            alg="ed25519",
            cover_content_digest=False,
        )

        app = _build_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.example.com") as client:
            response = await client.post(
                "/mcp/",
                content=body,
                headers={
                    "x-adcp-auth": signing_setup["access_token"],
                    "x-adcp-tenant": signing_setup["tenant_id"],
                    "content-type": "application/json",
                    **signed.as_dict(),
                },
            )

        assert response.status_code == 200, f"body: {response.text!r}"
        result = response.json()
        # Downstream handler MUST see the same bytes the verifier saw.
        assert result["body_seen"] == body.decode("utf-8")
        assert result["verified"] is True

    async def test_required_for_unsigned_rejected(self, session, keypair):
        """PR 2C required_for enforcement: an operation listed in
        TenantSigningPolicy.required_for that arrives unsigned → 401
        request_signature_required."""
        from tests.factories import (
            AdmittedOperatorFactory,
            OperatorAdvertiserLinkFactory,
            PrincipalFactory,
            TenantFactory,
            TenantSigningPolicyFactory,
        )

        os.environ["REPLAY_SWEEP_MODE"] = "off"
        from src.core.signing import bootstrap_replay_store

        bootstrap_replay_store()

        tenant = TenantFactory(tenant_id="t_required_for")
        operator = AdmittedOperatorFactory(
            tenant=tenant,
            operator_id="op_required",
            brand_json_url=BRAND_JSON_URL,
            is_trusted=False,
            is_active=True,
        )
        principal = PrincipalFactory(
            tenant=tenant,
            principal_id="p_required",
            access_token="required-token",
            bound_operator_id=operator.operator_id,
        )
        OperatorAdvertiserLinkFactory(operator=operator, principal=principal, is_active=True)
        TenantSigningPolicyFactory(
            tenant=tenant,
            enabled=True,
            required_for=["create_media_buy"],
        )
        session.commit()

        # Send the request UNSIGNED with method=tools/call name=create_media_buy.
        body = b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"create_media_buy","arguments":{}},"id":1}'
        app = _build_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.example.com") as client:
            response = await client.post(
                "/mcp/",
                content=body,
                headers={
                    "x-adcp-auth": "required-token",
                    "x-adcp-tenant": tenant.tenant_id,
                    "content-type": "application/json",
                },
            )

        assert response.status_code == 401, f"body: {response.text!r}"
        www = response.headers.get("www-authenticate", "")
        assert "request_signature_required" in www, www

    async def test_unbound_principal_cannot_skip_required_for(self, session, keypair):
        """H2 regression: a principal with bound_operator_id=NULL must NOT
        bypass required_for enforcement by skipping signing."""
        from tests.factories import (
            PrincipalFactory,
            TenantFactory,
            TenantSigningPolicyFactory,
        )

        os.environ["REPLAY_SWEEP_MODE"] = "off"
        from src.core.signing import bootstrap_replay_store

        bootstrap_replay_store()

        tenant = TenantFactory(tenant_id="t_unbound")
        # bound_operator_id intentionally left NULL.
        PrincipalFactory(
            tenant=tenant,
            principal_id="p_unbound",
            access_token="unbound-token",
        )
        TenantSigningPolicyFactory(
            tenant=tenant,
            enabled=True,
            required_for=["create_media_buy"],
        )
        session.commit()

        body = b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"create_media_buy","arguments":{}},"id":1}'
        app = _build_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.example.com") as client:
            response = await client.post(
                "/mcp/",
                content=body,
                headers={
                    "x-adcp-auth": "unbound-token",
                    "x-adcp-tenant": tenant.tenant_id,
                    "content-type": "application/json",
                },
            )

        assert response.status_code == 401, f"body: {response.text!r}"
        assert "request_signature_required" in response.headers.get("www-authenticate", "")

    async def test_disabled_link_rejects_signed_request(self, signing_setup, session):
        """H1 regression: deactivating operator_advertiser_link must stop the
        verifier from admitting signed requests on that link."""
        from src.core.database.models import OperatorAdvertiserLink

        # Disable the link the signing_setup fixture created.
        from sqlalchemy import select

        link = session.scalars(
            select(OperatorAdvertiserLink).filter_by(
                tenant_id=signing_setup["tenant_id"],
                operator_id=signing_setup["operator_id"],
                principal_id=signing_setup["principal_id"],
            )
        ).first()
        assert link is not None
        link.is_active = False
        session.commit()

        # Send a properly-signed request — should still reject because the
        # link is gone (verifier can't construct a binding).
        body = b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_products","arguments":{}},"id":1}'
        url = "http://t.example.com/mcp/"

        signed = sign_request(
            method="POST",
            url=url,
            headers={
                "host": "t.example.com",
                "content-type": "application/json",
                "x-adcp-auth": signing_setup["access_token"],
                "x-adcp-tenant": signing_setup["tenant_id"],
            },
            body=body,
            private_key=signing_setup["private_key"],
            key_id=signing_setup["key_id"],
            alg="ed25519",
            cover_content_digest=False,
        )

        app = _build_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.example.com") as client:
            response = await client.post(
                "/mcp/",
                content=body,
                headers={
                    "x-adcp-auth": signing_setup["access_token"],
                    "x-adcp-tenant": signing_setup["tenant_id"],
                    "content-type": "application/json",
                    **signed.as_dict(),
                },
            )

        # Signed request hits the "no binding" path → 401 request_signature_required.
        assert response.status_code == 401, f"body: {response.text!r}"

    async def test_required_for_other_op_unsigned_passes(self, session, keypair):
        """An operation NOT in required_for, sent unsigned → passes through."""
        from tests.factories import (
            AdmittedOperatorFactory,
            OperatorAdvertiserLinkFactory,
            PrincipalFactory,
            TenantFactory,
            TenantSigningPolicyFactory,
        )

        os.environ["REPLAY_SWEEP_MODE"] = "off"
        from src.core.signing import bootstrap_replay_store

        bootstrap_replay_store()

        tenant = TenantFactory(tenant_id="t_optional")
        operator = AdmittedOperatorFactory(
            tenant=tenant,
            operator_id="op_optional",
            brand_json_url=BRAND_JSON_URL,
            is_trusted=False,
            is_active=True,
        )
        principal = PrincipalFactory(
            tenant=tenant,
            principal_id="p_optional",
            access_token="optional-token",
            bound_operator_id=operator.operator_id,
        )
        OperatorAdvertiserLinkFactory(operator=operator, principal=principal, is_active=True)
        TenantSigningPolicyFactory(
            tenant=tenant,
            enabled=True,
            required_for=["create_media_buy"],
        )
        session.commit()

        # get_products is NOT in required_for → unsigned should pass.
        body = b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_products","arguments":{}},"id":1}'
        app = _build_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.example.com") as client:
            response = await client.post(
                "/mcp/",
                content=body,
                headers={
                    "x-adcp-auth": "optional-token",
                    "x-adcp-tenant": tenant.tenant_id,
                    "content-type": "application/json",
                },
            )

        assert response.status_code == 200, f"body: {response.text!r}"
        result = response.json()
        assert result["verified"] is False  # unsigned, no verified state

    async def test_trusted_operator_bypasses_verification(self, session, keypair):
        """is_trusted=True operator → middleware skips verifier even with
        signature headers present."""
        from tests.factories import (
            AdmittedOperatorFactory,
            OperatorAdvertiserLinkFactory,
            PrincipalFactory,
            TenantFactory,
        )

        # is_trusted bypass is gated on is_embedded — trust comes from the
        # network/header boundary, which only applies to embedded tenants.
        # The embedded-tenant guard requires the management-api caller flag
        # for is_embedded=True inserts; set it on the session to mirror what
        # tenant_management_api.provision_tenant does in production.
        session.info["management_api_caller"] = True
        tenant = TenantFactory(tenant_id="t_trusted_e2e", is_embedded=True)
        operator = AdmittedOperatorFactory(
            tenant=tenant,
            operator_id="embedded_host",
            brand_json_url=f"embedded://{tenant.tenant_id}/embedded_host",
            is_trusted=True,
            is_active=True,
        )
        principal = PrincipalFactory(
            tenant=tenant,
            principal_id="p_trusted",
            access_token="trusted-token",
            bound_operator_id=operator.operator_id,
        )
        OperatorAdvertiserLinkFactory(
            operator=operator,
            principal=principal,
            is_active=True,
        )
        session.commit()

        # Send a request WITH garbage signature headers; the trusted operator
        # path should never look at them.
        app = _build_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.example.com") as client:
            response = await client.post(
                "/mcp/",
                content=b'{"hello":"world"}',
                headers={
                    "x-adcp-auth": "trusted-token",
                    "x-adcp-tenant": tenant.tenant_id,
                    "content-type": "application/json",
                    "signature": "sig=:not-a-real-sig:",
                    "signature-input": 'sig=();keyid="fake";alg="ed25519";created=1;expires=2;nonce="x";tag="adcp-request-signing"',
                },
            )

        assert response.status_code == 200, f"body: {response.text!r}"
        # No verified state because we never ran the verifier.
        body = response.json()
        assert body["verified"] is False
