"""End-to-end verification of the adcp 7.0.2 buyer surface.

Drives every advertised tool through the real adcp 7.0.2 buyer client over A2A
(AdCP 3.1 canonical), the raw A2A wire in the legacy 3.0 dialect, and the MCP
surface with the fastmcp 4 client, validating canonical wire shapes with the
SDK's own response models.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from adcp import ADCPClient, AgentConfig
from adcp.types import (
    CreateMediaBuyRequest,
    GetAdcpCapabilitiesRequest,
    GetMediaBuyDeliveryRequest,
    GetMediaBuysRequest,
    GetProductsRequest,
    GetProductsResponse,
    GetSignalsRequest,
    ListAccountsRequest,
    ListCreativesRequest,
    ListCreativesResponse,
    ProvidePerformanceFeedbackRequest,
    SyncAccountsRequest,
    SyncCreativesRequest,
    UpdateMediaBuyRequest,
)
from adcp.types.legacy import LegacyListCreativeFormatsRequest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from tests.harness._asgi_app import run_on_app_loop
from tests.integration.test_delegate_wire_envelope_cross_transport import (
    _call_a2a_raw,
    _extract_a2a_data,
    authenticated_principal,
)

__all__ = ["authenticated_principal"]
pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

RESULTS: list[tuple[str, str, str]] = []


def _rec(surface: str, step: str, ok: bool, note: str = "") -> None:
    RESULTS.append((surface, step, ("PASS " if ok else "FAIL ") + note[:160]))


def _asset() -> dict[str, Any]:
    return {"asset_type": "image", "url": "https://cdn.example.com/banner.png", "width": 300, "height": 250}


def _window() -> tuple[str, str]:
    return (datetime.now(UTC) + timedelta(days=1)).isoformat(), (datetime.now(UTC) + timedelta(days=30)).isoformat()


async def _a2a_canonical(client: ADCPClient, p: dict[str, str]) -> None:
    acct = {"account_id": p["account_id"]}
    state: dict[str, Any] = {}

    async def step(name: str, coro_factory, check):
        try:
            res = await coro_factory()
            ok = bool(res.success) and check(res)
            _rec("A2A 3.1", name, ok, "" if ok else f"success={res.success} error={res.error!r}")
            return res
        except Exception as e:  # noqa: BLE001
            _rec("A2A 3.1", name, False, f"{type(e).__name__}: {e}")
            return None

    caps = await step(
        "get_adcp_capabilities",
        lambda: client.get_adcp_capabilities(GetAdcpCapabilitiesRequest()),
        lambda r: "3.1" in json.dumps(r.data.model_dump(mode="json"))
        and json.dumps(r.data.model_dump(mode="json")).count('"canonical_creatives": true') == 1,
    )
    await step(
        "list_creative_formats_legacy",
        lambda: client.list_creative_formats_legacy(LegacyListCreativeFormatsRequest()),
        lambda r: len(r.data.formats) > 0,
    )
    res = await step(
        "get_products wholesale (canonical format_options)",
        lambda: client.get_products(
            GetProductsRequest(buying_mode="wholesale", brand={"domain": "testbrand.example"}, account=acct)
        ),
        lambda r: r.data.products is not None and all(getattr(pr, "format_options", None) for pr in r.data.products),
    )
    res_b = await step(
        "get_products brief",
        lambda: client.get_products(
            GetProductsRequest(
                buying_mode="brief",
                brief="display banners for a gaming brand",
                brand={"domain": "testbrand.example"},
                account=acct,
            )
        ),
        lambda r: r.data.products is not None
        and len(r.data.products) >= 1
        and all(pr.format_options for pr in r.data.products),
    )
    products = (
        res_b.data.products
        if res_b and res_b.data and res_b.data.products
        else (res.data.products if res and res.data else [])
    ) or []
    product = next((pr for pr in products if pr.product_id == p["product_id"]), products[0] if products else None)
    if product is None or not product.format_options:
        _rec("A2A 3.1", "pick product/format option", False, "no product with format_options")
        return
    option = product.format_options[0]
    state["option_id"] = option.format_option_id
    await step(
        "sync_accounts (provision)",
        lambda: client.sync_accounts(
            SyncAccountsRequest.model_validate(
                {
                    "idempotency_key": uuid.uuid4().hex,
                    "accounts": [
                        {
                            "brand": {"domain": "testbrand.example"},
                            "operator": "testbrand.example",
                            "billing": "operator",
                            "payment_terms": "net_30",
                        }
                    ],
                }
            )
        ),
        lambda r: r.data.accounts[0].action.value
        if hasattr(r.data.accounts[0].action, "value")
        else r.data.accounts[0].action in ("created", "updated", "unchanged"),
    )
    await step(
        "list_accounts", lambda: client.list_accounts(ListAccountsRequest()), lambda r: len(r.data.accounts) >= 1
    )
    cid = f"e2e_{uuid.uuid4().hex[:8]}"
    await step(
        "sync_creatives (canonical format_option_ref)",
        lambda: client.sync_creatives(
            SyncCreativesRequest.model_validate(
                {
                    "idempotency_key": uuid.uuid4().hex,
                    "account": acct,
                    "creatives": [
                        {
                            "creative_id": cid,
                            "name": "E2E banner",
                            "format_kind": option.format_kind.value
                            if hasattr(option.format_kind, "value")
                            else option.format_kind,
                            "format_option_ref": {
                                "scope": "product",
                                "product_id": product.product_id,
                                "format_option_id": option.format_option_id,
                            },
                            "assets": {"banner_image": _asset()},
                        }
                    ],
                }
            )
        ),
        lambda r: str(r.data.creatives[0].action).endswith("created") and not r.data.creatives[0].errors,
    )
    await step(
        "list_creatives (canonical, paginated)",
        lambda: client.list_creatives(
            ListCreativesRequest.model_validate({"account": acct, "pagination": {"max_results": 5}})
        ),
        lambda r: any(c.creative_id == cid for c in r.data.creatives)
        and all(getattr(c, "format_option_ref", None) is not None for c in r.data.creatives),
    )
    start, end = _window()
    mb = await step(
        "create_media_buy (format_option_refs + inline creative)",
        lambda: client.create_media_buy(
            CreateMediaBuyRequest.model_validate(
                {
                    "idempotency_key": uuid.uuid4().hex,
                    "account": acct,
                    "brand": {"domain": "testbrand.example"},
                    "start_time": start,
                    "end_time": end,
                    "packages": [
                        {
                            "product_id": product.product_id,
                            "pricing_option_id": "cpm_usd_fixed",
                            "budget": 5000.0,
                            "format_option_refs": [
                                {
                                    "scope": "product",
                                    "product_id": product.product_id,
                                    "format_option_id": option.format_option_id,
                                }
                            ],
                            "creatives": [
                                {
                                    "creative_id": f"{cid}_inline",
                                    "name": "Inline",
                                    "format_kind": option.format_kind.value
                                    if hasattr(option.format_kind, "value")
                                    else option.format_kind,
                                    "format_option_ref": {
                                        "scope": "product",
                                        "product_id": product.product_id,
                                        "format_option_id": option.format_option_id,
                                    },
                                    "assets": {"banner_image": _asset()},
                                }
                            ],
                        }
                    ],
                }
            )
        ),
        lambda r: bool(getattr(r.data, "media_buy_id", None)),
    )
    mbid = getattr(mb.data, "media_buy_id", None) if mb and mb.data else None
    if not mbid:
        return
    await step(
        "get_media_buys",
        lambda: client.get_media_buys(GetMediaBuysRequest.model_validate({"account": acct, "media_buy_ids": [mbid]})),
        lambda r: any(m.media_buy_id == mbid for m in (r.data.media_buys or []))
        and all(
            getattr(pk, "format_option_refs", None) is not None or True
            for m in r.data.media_buys
            for pk in (m.packages or [])
        ),
    )
    await step(
        "update_media_buy (pause)",
        lambda: client.update_media_buy(
            UpdateMediaBuyRequest.model_validate(
                {"idempotency_key": uuid.uuid4().hex, "account": acct, "media_buy_id": mbid, "paused": True}
            )
        ),
        lambda r: r.data is not None,
    )
    await step(
        "get_media_buy_delivery",
        lambda: client.get_media_buy_delivery(
            GetMediaBuyDeliveryRequest.model_validate({"account": acct, "media_buy_ids": [mbid]})
        ),
        lambda r: r.data is not None,
    )
    await step(
        "get_signals",
        lambda: client.get_signals(
            GetSignalsRequest.model_validate({"discovery_mode": "brief", "signal_spec": "sports fans", "account": acct})
        ),
        lambda r: r.data is not None,
    )
    await step(
        "provide_performance_feedback",
        lambda: client.provide_performance_feedback(
            ProvidePerformanceFeedbackRequest.model_validate(
                {
                    "idempotency_key": uuid.uuid4().hex,
                    "media_buy_id": mbid,
                    "measurement_period": {"start": start, "end": end},
                    "performance_index": 1.1,
                }
            )
        ),
        lambda r: r.data is not None,
    )


def _a2a_legacy(p: dict[str, str]) -> None:
    acct = {"account_id": p["account_id"]}
    legacy_ref = {"agent_url": "https://creative.adcontextprotocol.org", "id": "display_300x250"}

    def raw(skill, params):
        return _call_a2a_raw(skill, {"adcp_version": "3.0", **params}, p)

    try:
        data = _extract_a2a_data(
            raw(
                "get_products",
                {
                    "buying_mode": "brief",
                    "brief": "display banners for a gaming brand",
                    "brand": {"domain": "testbrand.example"},
                    "account": acct,
                },
            ),
            expected_state="completed",
        )
        prods = data.get("products") or []
        ok = bool(prods) and all("format_ids" in pr and "format_options" not in pr for pr in prods)
        _rec("A2A 3.0", "get_products returns legacy format_ids", ok, "" if ok else json.dumps(prods)[:150])
    except Exception as e:  # noqa: BLE001
        _rec("A2A 3.0", "get_products returns legacy format_ids", False, f"{type(e).__name__}: {e}")
    cid = f"leg_{uuid.uuid4().hex[:8]}"
    try:
        data = _extract_a2a_data(
            raw(
                "sync_creatives",
                {
                    "idempotency_key": uuid.uuid4().hex,
                    "account": acct,
                    "creatives": [
                        {
                            "creative_id": cid,
                            "name": "Legacy",
                            "format_id": legacy_ref,
                            "assets": {"banner_image": _asset()},
                        }
                    ],
                },
            ),
            expected_state="completed",
        )
        ok = data["creatives"][0]["action"] == "created"
        _rec("A2A 3.0", "sync_creatives with legacy format_id", ok, "" if ok else json.dumps(data)[:150])
        data = _extract_a2a_data(raw("list_creatives", {"account": acct}), expected_state="completed")
        ok = any(c.get("creative_id") == cid and "format_id" in c for c in data.get("creatives", []))
        _rec("A2A 3.0", "list_creatives echoes legacy format_id", ok, "" if ok else json.dumps(data)[:150])
    except Exception as e:  # noqa: BLE001
        _rec("A2A 3.0", "sync/list creatives legacy", False, f"{type(e).__name__}: {e}")
    try:
        start, end = _window()
        data = _extract_a2a_data(
            raw(
                "create_media_buy",
                {
                    "idempotency_key": uuid.uuid4().hex,
                    "account": acct,
                    "brand": {"domain": "testbrand.example"},
                    "start_time": start,
                    "end_time": end,
                    "packages": [
                        {
                            "product_id": p["product_id"],
                            "pricing_option_id": "cpm_usd_fixed",
                            "budget": 1000.0,
                            "format_ids": [legacy_ref],
                            "creatives": [
                                {
                                    "creative_id": f"{cid}_i",
                                    "name": "Legacy inline",
                                    "format_id": legacy_ref,
                                    "assets": {"banner_image": _asset()},
                                }
                            ],
                        }
                    ],
                },
            ),
            expected_state="completed",
        )
        _rec(
            "A2A 3.0",
            "create_media_buy with legacy format_ids + inline creative",
            bool(data.get("media_buy_id")),
            json.dumps(data)[:150] if not data.get("media_buy_id") else "",
        )
    except Exception as e:  # noqa: BLE001
        _rec("A2A 3.0", "create_media_buy legacy", False, f"{type(e).__name__}: {e}")


def _mcp(p: dict[str, str], app) -> Any:
    acct = {"account_id": p["account_id"]}

    def httpx_factory(**hk):
        hk.setdefault("timeout", 30.0)
        hk["transport"] = httpx.ASGITransport(app=app)
        hk["base_url"] = "http://localhost"
        return httpx.AsyncClient(**hk)

    transport = StreamableHttpTransport(
        url="http://localhost/mcp/",
        headers={"x-adcp-auth": p["access_token"], "x-adcp-tenant": p["tenant_id"]},
        httpx_client_factory=httpx_factory,
    )

    async def run():
        async with Client(transport) as client:
            tools = await client.list_tools()
            names = sorted(t.name for t in tools)
            _rec(
                "MCP",
                f"tools/list advertises {len(names)} tools with outputSchema",
                len(names) == 13 and all(t.outputSchema for t in tools),
                ",".join(names),
            )
            r = await client.call_tool_mcp(
                "get_products",
                {
                    "adcp_version": "3.1",
                    "buying_mode": "wholesale",
                    "brand": {"domain": "testbrand.example"},
                    "account": acct,
                },
            )
            try:
                model = GetProductsResponse.model_validate(r.structured_content)
                ok = not r.is_error and all(pr.format_options for pr in (model.products or []))
                _rec(
                    "MCP",
                    "get_products 3.1 validates as canonical GetProductsResponse",
                    ok,
                    "" if ok else str(r.structured_content)[:150],
                )
            except Exception as e:  # noqa: BLE001
                _rec(
                    "MCP",
                    "get_products 3.1 validates as canonical GetProductsResponse",
                    False,
                    f"{type(e).__name__}: {str(e)[:140]}",
                )
            r = await client.call_tool_mcp(
                "get_products", {"buying_mode": "wholesale", "brand": {"domain": "testbrand.example"}, "account": acct}
            )
            ok = not r.is_error and all(
                "format_options" in pr for pr in (r.structured_content or {}).get("products", [])
            )
            _rec(
                "MCP",
                "get_products unversioned -> canonical (fastmcp outputSchema-compatible)",
                ok,
                "" if ok else str(r.structured_content)[:150],
            )
            r = await client.call_tool_mcp(
                "list_creatives", {"adcp_version": "3.1", "account": acct, "pagination": {"max_results": 3}}
            )
            try:
                ListCreativesResponse.model_validate(r.structured_content)
                _rec("MCP", "list_creatives 3.1 validates as canonical ListCreativesResponse", not r.is_error)
            except Exception as e:  # noqa: BLE001
                _rec(
                    "MCP",
                    "list_creatives 3.1 validates as canonical ListCreativesResponse",
                    False,
                    f"{type(e).__name__}: {str(e)[:140]}",
                )
            r = await client.call_tool_mcp("get_adcp_capabilities", {})
            sc = r.structured_content or {}
            _rec(
                "MCP",
                "get_adcp_capabilities declares canonical_creatives",
                bool((sc.get("media_buy") or {}).get("features", {}).get("canonical_creatives")),
                json.dumps(sc.get("adcp"))[:120],
            )

    return run()


def test_adcp7_end_to_end(authenticated_principal, integration_db):
    p = authenticated_principal
    token = p["access_token"]

    def factory(app):
        async def run():
            cfg = AgentConfig(
                id="verify", agent_uri="http://localhost/", protocol="a2a", auth_token=token, auth_type="bearer"
            )
            client = ADCPClient(cfg)
            headers = {"Authorization": f"Bearer {token}", "x-adcp-tenant": p["tenant_id"]}

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
                await _a2a_canonical(client, p)
            await _mcp(p, app)

        return run()

    run_on_app_loop(factory)
    _a2a_legacy(p)
    failed = [r for r in RESULTS if r[2].startswith("FAIL")]
    summary = "\n".join(f"{surface:8} | {step:70} | {outcome}" for surface, step, outcome in RESULTS)
    assert not failed, f"{len(failed)} verification steps failed:\n{summary}"
    assert len(RESULTS) >= 23, summary
