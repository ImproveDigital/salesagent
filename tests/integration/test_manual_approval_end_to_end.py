"""End-to-end manual-approval lifecycle against the in-process server.

Drives the buyer side with the real ``adcp`` 8 ``ADCPClient`` over A2A and the
seller side with the Flask admin routes a human operator uses:

1. ``create_media_buy`` on a ``human_review_required`` tenant, with inline
   creatives and a buyer ``push_notification_config`` → completed envelope
   carrying ``media_buy_id`` and ``media_buy_status=pending_start``; the buy is
   persisted as ``pending_approval`` with a ``requires_approval`` workflow step.
2. ``list_creatives`` shows the inline creative awaiting review.
3. Operator approves the creative, then approves the workflow step → the
   adapter order is created and the buy leaves ``pending_approval``.
4. The buyer receives the terminal ``create_media_buy`` task webhook on the
   registered URL, and ``list_creatives`` now reports the creative approved.

This is the flow the adcp 8 upgrade could not exercise through unit tests
(the human-approval path returns a synchronous success envelope, not a
hand-rolled ``submitted`` one, which is exactly what this test pins down).

Run: ``scripts/run-test.sh tests/integration/test_manual_approval_end_to_end.py -x -v``
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from adcp import ADCPClient, AgentConfig
from adcp.types import (
    CreateMediaBuyRequest,
    GetMediaBuysRequest,
    GetProductsRequest,
    ListCreativesRequest,
)
from sqlalchemy import select

from src.core.database.database_session import get_db_session
from src.core.database.models import Creative as CreativeRow
from src.core.database.models import MediaBuy as MediaBuyRow
from src.core.database.models import ObjectWorkflowMapping, WorkflowStep
from tests.harness._asgi_app import run_on_app_loop
from tests.integration.test_delegate_wire_envelope_cross_transport import provision_wire_principal

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

WEBHOOK_WAIT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manual_approval_principal(integration_db):
    """Tenant that requires a human to approve every media buy."""
    yield from provision_wire_principal(human_review_required=True, approval_mode="require-human")


class _WebhookReceiver:
    """Loopback HTTP server that records every POST body it receives."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                receiver.received.append({"path": self.path, "headers": dict(self.headers), "body": body})
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args: Any) -> None:  # silence the default stderr log
                return

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/adcp/webhook"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> _WebhookReceiver:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def wait_for(self, predicate, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for hit in self.received:
                if predicate(hit):
                    return hit
            time.sleep(0.1)
        return None


@pytest.fixture
def webhook_receiver():
    receiver = _WebhookReceiver().start()
    try:
        yield receiver
    finally:
        receiver.stop()


# ---------------------------------------------------------------------------
# Buyer-side helpers (real adcp client, routed into the ASGI app)
# ---------------------------------------------------------------------------


def _asset() -> dict[str, Any]:
    return {"asset_type": "image", "url": "https://cdn.example.com/banner.png", "width": 300, "height": 250}


def _window() -> tuple[str, str]:
    start = datetime.now(UTC) + timedelta(days=1)
    return start.isoformat(), (start + timedelta(days=29)).isoformat()


def _run_as_buyer(principal: dict[str, str], coro_fn):
    """Run ``coro_fn(client, acct)`` with a real ``ADCPClient`` bound to the in-process app."""
    token = principal["access_token"]

    def factory(app):
        async def run():
            cfg = AgentConfig(
                id="manual-approval-e2e",
                agent_uri="http://localhost/",
                protocol="a2a",
                auth_token=token,
                auth_type="bearer",
            )
            client = ADCPClient(cfg)
            headers = {"Authorization": f"Bearer {token}", "x-adcp-tenant": principal["tenant_id"]}

            async def _get_httpx_client(self):
                if self._httpx_client is None:
                    self._httpx_client = httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://localhost",
                        headers=headers,
                        timeout=60.0,
                    )
                return self._httpx_client

            with patch.object(type(client.adapter), "_get_httpx_client", _get_httpx_client):
                return await coro_fn(client, {"account_id": principal["account_id"]})

        return run()

    return run_on_app_loop(factory)


def _unwrap(result, step: str):
    assert result.success, f"{step} failed: {result.error!r}"
    return result.data


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True) if hasattr(model, "model_dump") else dict(model)


# ---------------------------------------------------------------------------
# Seller-side helpers (database + admin routes)
# ---------------------------------------------------------------------------


def _db_media_buy(media_buy_id: str) -> dict[str, Any]:
    with get_db_session() as session:
        row = session.scalars(select(MediaBuyRow).filter_by(media_buy_id=media_buy_id)).first()
        assert row is not None, f"media buy {media_buy_id} not persisted"
        return {"status": row.status, "approved_at": row.approved_at, "tenant_id": row.tenant_id}


def _db_approval_step(media_buy_id: str) -> dict[str, Any]:
    with get_db_session() as session:
        mapping = session.scalars(
            select(ObjectWorkflowMapping).filter_by(object_type="media_buy", object_id=media_buy_id)
        ).first()
        assert mapping is not None, f"no workflow mapping for media buy {media_buy_id}"
        step = session.scalars(select(WorkflowStep).filter_by(step_id=mapping.step_id)).first()
        assert step is not None
        return {"step_id": step.step_id, "context_id": step.context_id, "status": step.status}


def _db_creative_status(creative_id: str) -> str | None:
    with get_db_session() as session:
        row = session.scalars(select(CreativeRow).filter_by(creative_id=creative_id)).first()
        return row.status if row is not None else None


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
def test_manual_approval_lifecycle_with_creatives_and_webhook(
    manual_approval_principal,
    authenticated_admin_client,
    webhook_receiver,
):
    p = manual_approval_principal
    tenant_id = p["tenant_id"]
    creative_id = f"e2e_inline_{uuid.uuid4().hex[:8]}"
    operation_id = f"op_{uuid.uuid4().hex[:12]}"
    start, end = _window()

    # ---- 1. Buyer creates a media buy with an inline creative -------------
    async def create(client: ADCPClient, acct: dict[str, str]) -> dict[str, Any]:
        products = (
            _unwrap(
                await client.get_products(
                    GetProductsRequest(buying_mode="wholesale", brand={"domain": "testbrand.example"}, account=acct)
                ),
                "get_products",
            ).products
            or []
        )
        if not any(pr.product_id == p["product_id"] for pr in products):
            products = (
                _unwrap(
                    await client.get_products(
                        GetProductsRequest(
                            buying_mode="brief",
                            brief="display banners for a gaming brand",
                            brand={"domain": "testbrand.example"},
                            account=acct,
                        )
                    ),
                    "get_products (brief)",
                ).products
                or []
            )
        assert products, "get_products returned no products for the test tenant"
        product = next((pr for pr in products if pr.product_id == p["product_id"]), products[0])
        assert product.format_options, f"product {product.product_id} has no canonical format_options"
        option = product.format_options[0]
        option_ref = {
            "scope": "product",
            "product_id": product.product_id,
            "format_option_id": option.format_option_id,
        }
        req = CreateMediaBuyRequest.model_validate(
            {
                "idempotency_key": uuid.uuid4().hex,
                "account": acct,
                "brand": {"domain": "testbrand.example"},
                "start_time": start,
                "end_time": end,
                "push_notification_config": {
                    "url": webhook_receiver.url,
                    "operation_id": operation_id,
                    # Legacy HMAC keeps the registration independent of a tenant
                    # RFC 9421 signing credential (``legacy_hmac_fallback`` is advertised).
                    "authentication": {"schemes": ["HMAC-SHA256"], "credentials": "e2e-shared-secret-" + "x" * 24},
                },
                "packages": [
                    {
                        "product_id": product.product_id,
                        "pricing_option_id": "cpm_usd_fixed",
                        "budget": 5000.0,
                        "format_option_refs": [option_ref],
                        "creatives": [
                            {
                                "creative_id": creative_id,
                                "name": "E2E inline banner",
                                "format_kind": getattr(option.format_kind, "value", option.format_kind),
                                "format_option_ref": option_ref,
                                "assets": {"banner_image": _asset()},
                            }
                        ],
                    }
                ],
            }
        )
        return _dump(_unwrap(await client.create_media_buy(req), "create_media_buy"))

    created = _run_as_buyer(p, create)
    media_buy_id = created["media_buy_id"]
    assert media_buy_id, created
    # Human approval is a *synchronous* success envelope with the lifecycle
    # status telling the buyer what blocks activation — never a hand-rolled
    # ``submitted`` task (adcp 8 rejects those at the dispatcher).
    assert created["status"] == "completed", created
    assert created["media_buy_status"] == "pending_start", created

    assert _db_media_buy(media_buy_id)["status"] == "pending_approval"
    step = _db_approval_step(media_buy_id)
    assert step["status"] == "requires_approval", step

    # ---- 2. Buyer sees the inline creative awaiting review -----------------
    async def list_creative(client: ADCPClient, acct: dict[str, str]) -> dict[str, Any]:
        data = _unwrap(
            await client.list_creatives(
                ListCreativesRequest.model_validate({"account": acct, "filters": {"creative_ids": [creative_id]}})
            ),
            "list_creatives",
        )
        rows = [_dump(c) for c in data.creatives]
        assert rows, "inline creative not listed"
        return rows[0]

    before = _run_as_buyer(p, list_creative)
    assert before["creative_id"] == creative_id
    assert before["status"] != "approved", before
    assert _db_creative_status(creative_id) == "pending_review"

    # ---- 3. Operator approves the creative, then the media buy -------------
    resp = authenticated_admin_client.post(
        f"/tenant/{tenant_id}/creatives/review/{creative_id}/approve",
        json={"approved_by": "e2e-operator@example.com"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert _db_creative_status(creative_id) == "approved"

    resp = authenticated_admin_client.post(
        f"/tenant/{tenant_id}/workflows/{step['context_id']}/steps/{step['step_id']}/approve",
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json().get("success") is True, resp.get_json()

    after = _db_media_buy(media_buy_id)
    assert after["status"] in {"scheduled", "active", "pending_ad_server_approval"}, after
    assert after["approved_at"] is not None

    # ---- 4. Buyer receives the terminal task webhook ------------------------
    hit = webhook_receiver.wait_for(lambda h: media_buy_id in json.dumps(h["body"]), WEBHOOK_WAIT_SECONDS)
    assert hit is not None, f"no create_media_buy decision webhook received; got {webhook_receiver.received}"
    payload = json.dumps(hit["body"])
    assert "completed" in payload, payload
    # The buyer spoke A2A, so the callback is an A2A ``Task`` carrying the
    # create_media_buy result as its artifact.
    assert '"kind": "task"' in payload or '"status"' in payload, payload

    # ---- 5. Buyer-visible state after approval -------------------------------
    after_creative = _run_as_buyer(p, list_creative)
    assert after_creative["status"] == "approved", after_creative

    async def get_buys(client: ADCPClient, acct: dict[str, str]) -> list[dict[str, Any]]:
        data = _unwrap(
            await client.get_media_buys(
                GetMediaBuysRequest.model_validate({"account": acct, "media_buy_ids": [media_buy_id]})
            ),
            "get_media_buys",
        )
        return [_dump(m) for m in (data.media_buys or [])]

    buys = _run_as_buyer(p, get_buys)
    assert [b["media_buy_id"] for b in buys] == [media_buy_id], buys
    # The flight has not started yet, so the wire lifecycle status stays
    # ``pending_start``; approval is observable through the webhook above
    # and through the now-approved creative.
    assert buys[0]["status"] == "pending_start", buys[0]
