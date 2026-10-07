#!/usr/bin/env python
"""Drive the manual-approval media-buy lifecycle against a running sales agent (dev/local).

Buyer side only — it uses the real ``adcp`` client exactly like a buyer agent
would. The human approval happens in the Admin UI (creative review + workflow
approval); this script creates the buy, tells you where to approve it, then
polls until the buyer-visible state shows the approval landed.

Steps
-----
1. ``get_adcp_capabilities`` / ``get_products`` → pick a product with canonical
   ``format_options``.
2. ``create_media_buy`` on the manual-approval tenant with ONE inline creative
   (and an optional ``push_notification_config`` when ``--webhook-url`` is given).
   Expect: ``status=completed``, ``media_buy_status=pending_start``, ``media_buy_id``.
3. Poll ``list_creatives`` (creative ``status``) and ``get_media_buys`` until the
   creative is ``approved`` or ``--wait-minutes`` elapses.

Examples
--------
    # dev, over A2A (host root), buyer token from Admin UI → Advertisers → API Token
    uv run python scripts/manual_approval_e2e.py \\
        --base-url https://azerion-gaming.sales-agent.dev.fms.azeriondev.com \\
        --token "$DEV_SALES_AGENT_TOKEN" --wait-minutes 20

    # local docker stack (nginx on :8000), over MCP
    uv run python scripts/manual_approval_e2e.py --base-url http://localhost:8000 \\
        --protocol mcp --token "$LOCAL_SALES_AGENT_TOKEN"

The tenant must have *Human review required* enabled (Admin UI → tenant
settings), otherwise the buy auto-approves and the script reports that.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from adcp import ADCPClient, AgentConfig
from adcp.types import (
    CreateMediaBuyRequest,
    GetAdcpCapabilitiesRequest,
    GetMediaBuysRequest,
    GetProductsRequest,
    ListAccountsRequest,
    ListCreativesRequest,
)


def _say(ok: bool, step: str, note: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {step}" + (f"  — {note}" if note else ""), flush=True)


# Named scenarios: defaults for a tenant + product variation. Any CLI flag
# given explicitly overrides the scenario value. Add rows here to cover more
# ad servers / products; ``--scenario`` picks one.
SCENARIOS: dict[str, dict[str, Any]] = {
    "azerion-gaming-gam-display": {
        "base_url": "https://azerion-gaming.sales-agent.dev.fms.azeriondev.com",
        "tenant": "azerion-gaming",
        "adserver": "Google Ad Manager",
        "product_id": "prod_ea17a482",
        "brand_domain": "azerion.com",
        "brief": "A 2 day display campaign for azerion.com starting tomorrow",
        "budget": 2.0,
        "days": 2,
        "asset_url": "https://hb.improvedigital.com/creatives/display/300x250.jpg",
        "width": 300,
        "height": 250,
        "click_url": "https://azerion.com",
        "adcp_version": "3.1",
    },
    "viva-gaming-improve-display": {
        "base_url": "https://viva-gaming.sales-agent.dev.fms.azeriondev.com",
        "tenant": "viva-gaming",
        "adserver": "Improve Digital",
        "product_id": "prod_1f86ac53",
        "brand_domain": "azerion.com",
        "brief": "A 2 day display campaign for azerion.com starting tomorrow",
        "budget": 2.0,
        "days": 2,
        "asset_url": "https://hb.improvedigital.com/creatives/display/300x250.jpg",
        "width": 300,
        "height": 250,
        "click_url": "https://azerion.com",
        "adcp_version": "3.1",
    },
}


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True) if hasattr(model, "model_dump") else dict(model)


def _client(args: argparse.Namespace) -> ADCPClient:
    base = args.base_url.rstrip("/")
    agent_uri = base + "/" if args.protocol == "a2a" else base + "/mcp/"
    kwargs: dict[str, Any] = {
        "id": "manual-approval-e2e",
        "agent_uri": agent_uri,
        "protocol": args.protocol,
        "auth_token": args.token,
        "auth_type": "bearer",
    }
    if args.protocol == "mcp":
        # The adcp client sends ``x-adcp-auth: Bearer <tok>`` by default; the
        # standard header works on every salesagent deployment.
        kwargs["auth_header"] = "Authorization"
    # adcp 8 clients pin AdCP 3.2 by default; a seller still on adcp 7 (spec
    # 3.1) rejects that, so ``--adcp-version 3.1`` keeps the run possible there.
    return ADCPClient(AgentConfig(**kwargs), adcp_version=args.adcp_version)


async def _pick_account(client: ADCPClient, args: argparse.Namespace) -> dict[str, str]:
    if args.account_id:
        return {"account_id": args.account_id}
    res = await client.list_accounts(ListAccountsRequest())
    if not res.success or not res.data or not res.data.accounts:
        raise SystemExit(f"list_accounts failed or returned no accounts: {res.error!r}; pass --account-id")
    account_id = res.data.accounts[0].account_id
    _say(True, "list_accounts", f"using account {account_id}")
    return {"account_id": str(account_id)}


async def _pick_product(client: ADCPClient, acct: dict[str, str], args: argparse.Namespace):
    brand = {"domain": args.brand_domain}

    async def _query(mode: str) -> list[Any]:
        kwargs: dict[str, Any] = {"buying_mode": mode, "brand": brand, "account": acct}
        if mode == "brief":
            kwargs["brief"] = args.brief
        res = await client.get_products(GetProductsRequest(**kwargs))
        if not res.success:
            _say(False, f"get_products ({mode})", repr(res.error)[:200])
            return []
        return list(res.data.products or []) if res.data else []

    wanted = args.product_id
    products = await _query("wholesale")
    # Brief-only products (e.g. guaranteed bundles) never show up in the
    # wholesale feed, so also ask the brief surface when the requested
    # product is missing or nothing came back at all.
    if not products or (wanted and not any(p.product_id == wanted for p in products)):
        products += await _query("brief")
    if not products:
        raise SystemExit("get_products returned no products for this account/brand")
    if wanted:
        product = next((p for p in products if p.product_id == wanted), None)
        if product is None:
            raise SystemExit(
                f"product {wanted!r} not offered to this account/brand; available: {[p.product_id for p in products]}"
            )
    else:
        product = next((p for p in products if getattr(p, "format_options", None)), None)
    if product is None or not product.format_options:
        raise SystemExit("no product with canonical format_options; cannot attach an inline creative")
    option = _pick_format_option(product, args)
    pricing_options = [_dump(po) for po in (product.pricing_options or [])]
    chosen: dict[str, Any] | None = None
    if args.pricing_option_id:
        chosen = next((po for po in pricing_options if po.get("pricing_option_id") == args.pricing_option_id), None)
    if chosen is None:
        # Prefer a fixed-rate option (no bid needed); fall back to the first one.
        chosen = next(
            (po for po in pricing_options if _is_fixed_pricing(po)), pricing_options[0] if pricing_options else None
        )
    pricing = chosen.get("pricing_option_id") if chosen else None
    note = "" if chosen is None or _is_fixed_pricing(chosen) else " (auction: bid_price will be sent)"
    _say(True, "get_products", f"product={product.product_id} option={option.format_option_id} pricing={pricing}{note}")
    return product, option, pricing, chosen


def _pick_format_option(product, args: argparse.Namespace):
    """Prefer an ``image`` option matching --width/--height; else the first option."""
    options = list(product.format_options or [])
    for opt in options:
        od = _dump(opt)
        params = od.get("params") or {}
        kind = str(od.get("format_kind") or "")
        if (
            kind == "image"
            and int(params.get("width") or 0) == args.width
            and int(params.get("height") or 0) == args.height
        ):
            return opt
    return options[0]


def _is_fixed_pricing(po: dict[str, Any]) -> bool:
    if po.get("is_fixed") is True or po.get("fixed_price") is not None or po.get("rate") is not None:
        return True
    return str(po.get("pricing_option_id", "")).endswith("_fixed")


def _bid_price_for(po: dict[str, Any] | None, requested: float | None) -> float:
    if requested is not None:
        return requested
    floor = float((po or {}).get("floor_price") or 0.0)
    guidance = (po or {}).get("price_guidance") or {}
    hint = (guidance.get("p50") or guidance.get("floor")) if isinstance(guidance, dict) else None
    return max(floor, float(hint) if hint else 0.0, 1.0)


async def _create(
    client: ADCPClient,
    acct: dict[str, str],
    product,
    option,
    pricing: str,
    pricing_option: dict[str, Any] | None,
    args,
) -> dict[str, Any]:
    start = datetime.now(UTC) + timedelta(days=1)
    end = start + timedelta(days=args.days)
    creative_id = f"e2e_inline_{uuid.uuid4().hex[:8]}"
    option_ref = {"scope": "product", "product_id": product.product_id, "format_option_id": option.format_option_id}
    assets: dict[str, Any] = {
        "banner_image": {"asset_type": "image", "url": args.asset_url, "width": args.width, "height": args.height},
    }
    if args.click_url:
        # ``click_url`` is one of the clickthrough asset ids the server's
        # creative helpers fall back to when the format spec has no url slot.
        assets["click_url"] = {"asset_type": "url", "url": args.click_url}
    package: dict[str, Any] = {
        "product_id": product.product_id,
        "pricing_option_id": pricing,
        "budget": args.budget,
        "format_option_refs": [option_ref],
        "creatives": [
            {
                "creative_id": creative_id,
                "name": "Manual-approval e2e banner",
                "format_kind": getattr(option.format_kind, "value", option.format_kind),
                "format_option_ref": option_ref,
                "assets": assets,
            }
        ],
    }
    if pricing_option is not None and not _is_fixed_pricing(pricing_option):
        package["bid_price"] = _bid_price_for(pricing_option, args.bid_price)
    body: dict[str, Any] = {
        "idempotency_key": uuid.uuid4().hex,
        "account": acct,
        "brand": {"domain": args.brand_domain},
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "packages": [package],
    }
    if args.webhook_url:
        body["push_notification_config"] = {
            "url": args.webhook_url,
            "operation_id": f"op_{uuid.uuid4().hex[:12]}",
            "authentication": {"schemes": ["HMAC-SHA256"], "credentials": args.webhook_secret},
        }
    res = await client.create_media_buy(CreateMediaBuyRequest.model_validate(body))
    if not res.success or res.data is None:
        raise SystemExit(f"create_media_buy failed: {res.error!r}")
    data = _dump(res.data)
    data["_creative_id"] = creative_id
    return data


async def _poll(client: ADCPClient, acct: dict[str, str], media_buy_id: str, creative_id: str, args) -> bool:
    deadline = time.monotonic() + args.wait_minutes * 60
    last = ""
    while True:
        cr = await client.list_creatives(
            ListCreativesRequest.model_validate({"account": acct, "filters": {"creative_ids": [creative_id]}})
        )
        creative_status = None
        if cr.success and cr.data and cr.data.creatives:
            creative_status = str(_dump(cr.data.creatives[0]).get("status"))
        mb = await client.get_media_buys(
            GetMediaBuysRequest.model_validate({"account": acct, "media_buy_ids": [media_buy_id]})
        )
        buy_status = None
        if mb.success and mb.data and mb.data.media_buys:
            buy_status = str(_dump(mb.data.media_buys[0]).get("status"))
        line = f"creative={creative_status} media_buy={buy_status}"
        if line != last:
            print(f"      {datetime.now(UTC).strftime('%H:%M:%S')}  {line}", flush=True)
            last = line
        if creative_status == "approved":
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(args.poll_seconds)


async def _main(args: argparse.Namespace) -> int:
    client = _client(args)
    try:
        caps = await client.get_adcp_capabilities(GetAdcpCapabilitiesRequest())
        _say(bool(caps.success), "get_adcp_capabilities", "" if caps.success else repr(caps.error))
        acct = await _pick_account(client, args)
        if args.resume_media_buy_id:
            # A buy from an earlier run is still waiting for the operator:
            # skip creation and only poll until the approval is visible.
            creative_id = args.resume_creative_id or ""
            print(f"Resuming: polling {args.resume_media_buy_id} / creative {creative_id or '<none>'} …")
            ok = await _poll(client, acct, args.resume_media_buy_id, creative_id, args)
            _say(ok, "approval observed by the buyer", "creative approved" if ok else "timed out waiting for approval")
            return 0 if ok else 2
        product, option, pricing, pricing_option = await _pick_product(client, acct, args)
        if not pricing:
            raise SystemExit("no pricing option available; pass --pricing-option-id")
        if args.dry_run:
            _say(True, "dry run", "connectivity, account and product discovery verified; no media buy created")
            return 0

        created = await _create(client, acct, product, option, pricing, pricing_option, args)
        media_buy_id = created.get("media_buy_id")
        creative_id = created.pop("_creative_id")
        pending = created.get("media_buy_status") in {"pending_start", "pending_creatives"}
        _say(
            bool(media_buy_id) and created.get("status") == "completed",
            "create_media_buy",
            f"media_buy_id={media_buy_id} status={created.get('status')} media_buy_status={created.get('media_buy_status')}",
        )
        if not media_buy_id:
            print(json.dumps(created, indent=2))
            return 1
        if not pending:
            _say(
                False,
                "manual approval expected",
                "the buy did not land in a pending state — is Human review required on?",
            )

        base = args.base_url.rstrip("/")
        tenant_hint = f"/tenant/{args.tenant}" if args.tenant else "/tenant/<tenant>"
        print()
        print("Now approve in the Admin UI:")
        print(f"  1. Creatives → review → approve creative {creative_id}")
        print(f"      {base}{tenant_hint}/creatives/review")
        print(f"  2. Workflows → approve the create_media_buy step for {media_buy_id}")
        print(f"      {base}{tenant_hint}/workflows")
        print(f"Polling buyer-visible state every {args.poll_seconds}s for up to {args.wait_minutes} min …")
        ok = await _poll(client, acct, media_buy_id, creative_id, args)
        _say(ok, "approval observed by the buyer", "creative approved" if ok else "timed out waiting for approval")
        if args.webhook_url:
            print(f"Check your receiver at {args.webhook_url} for the completed create_media_buy task webhook.")
        return 0 if ok else 2
    finally:
        close = getattr(client, "close", None) or getattr(client, "aclose", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


def _parse() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), help="Named tenant/product scenario (see SCENARIOS)")
    ap.add_argument("--base-url", help="Sales agent base URL (A2A at /, MCP at /mcp/); scenario default if omitted")
    ap.add_argument("--token", required=True, help="Buyer (advertiser) bearer token for that tenant")
    ap.add_argument("--protocol", choices=("a2a", "mcp"), default="a2a")
    ap.add_argument("--tenant", help="Tenant id, only used to print Admin UI links")
    ap.add_argument("--account-id", help="Account to buy under (default: first list_accounts result)")
    ap.add_argument("--product-id", help="Product to buy (default: first product with format_options)")
    ap.add_argument("--pricing-option-id", help="Pricing option (default: product's first)")
    ap.add_argument("--brand-domain", default=None)
    ap.add_argument("--brief", default=None)
    ap.add_argument("--budget", type=float, default=None, help="Total budget (default 5000, or the scenario's)")
    ap.add_argument("--days", type=int, default=None, help="Flight length in days from tomorrow (default 29)")
    ap.add_argument("--click-url", default=None, help="Click-through URL attached as a url asset")
    ap.add_argument("--bid-price", type=float, help="CPM bid for auction pricing (default: max(floor, guidance, 1.0))")
    ap.add_argument("--asset-url", default=None)
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--webhook-url", help="Public HTTPS receiver for the task webhook (optional)")
    ap.add_argument("--webhook-secret", default="manual-approval-e2e-shared-secret-32chars")
    ap.add_argument("--dry-run", action="store_true", help="Stop after product discovery; create nothing")
    ap.add_argument("--resume-media-buy-id", help="Poll an existing buy from an earlier run instead of creating one")
    ap.add_argument("--resume-creative-id", help="Creative id to watch together with --resume-media-buy-id")
    ap.add_argument("--adcp-version", help="Pin the AdCP wire version (e.g. 3.1 for an adcp 7 seller; default 3.2)")
    ap.add_argument("--wait-minutes", type=float, default=15.0)
    ap.add_argument("--poll-seconds", type=float, default=10.0)
    return _apply_scenario_defaults(ap.parse_args())


_GENERIC_DEFAULTS: dict[str, Any] = {
    "base_url": None,
    "tenant": None,
    "product_id": None,
    "brand_domain": "testbrand.example",
    "brief": "display banners for a gaming brand",
    "budget": 5000.0,
    "days": 29,
    "asset_url": "https://cdn.example.com/banner.png",
    "width": 300,
    "height": 250,
    "click_url": None,
    "adcp_version": None,
}


def _apply_scenario_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Fill unset flags from the chosen scenario, then from generic defaults."""
    scenario = SCENARIOS.get(args.scenario or "", {})
    for key, generic in _GENERIC_DEFAULTS.items():
        if getattr(args, key, None) is None:
            setattr(args, key, scenario.get(key, generic))
    if not args.base_url:
        raise SystemExit("--base-url is required (or pick a --scenario that defines one)")
    if scenario:
        print(f"Scenario {args.scenario}: {scenario['adserver']} tenant {args.tenant}, product {args.product_id}")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
