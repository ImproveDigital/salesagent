"""MCP capability sweep: call every tool the server advertises over MCP.

Drives the in-process ASGI app with the real ``fastmcp`` client (streamable
HTTP over MCP), exactly like a generic MCP buyer would:

1. ``tools/list`` — every advertised tool must carry an ``outputSchema``.
2. One call per advertised tool with a minimal valid request. Calls are
   ordered so later tools can use ids produced by earlier ones (product →
   creative → media buy → update/delivery/feedback).
3. Every structured result is validated against the tool's advertised
   ``outputSchema`` with ``jsonschema`` — the same check strict MCP clients
   perform before handing the result to the buyer.
4. Any advertised tool without a payload in :data:`PAYLOADS` is still
   invoked with ``{}`` so a new tool cannot silently escape coverage; it is
   reported as ``NO-PAYLOAD`` and fails the sweep.

A per-tool table is printed at the end (run with ``-s`` to see it on a pass).

Run: ``scripts/run-test.sh tests/integration/test_mcp_capability_sweep.py -x -s``
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jsonschema
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from tests.harness._asgi_app import run_on_app_loop
from tests.integration.test_delegate_wire_envelope_cross_transport import authenticated_principal

__all__ = ["authenticated_principal"]
pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

ADCP_VERSION = "3.1"


def _asset() -> dict[str, Any]:
    return {"asset_type": "image", "url": "https://cdn.example.com/banner.png", "width": 300, "height": 250}


def _window() -> tuple[str, str]:
    start = datetime.now(UTC) + timedelta(days=1)
    return start.isoformat(), (start + timedelta(days=29)).isoformat()


# ---------------------------------------------------------------------------
# Payload builders. Each receives the shared ``state`` dict (ids produced by
# earlier calls) and the account reference; returns the tool arguments.
# Order matters: later entries consume ids produced by earlier ones.
# ---------------------------------------------------------------------------

Builder = Callable[[dict[str, Any], dict[str, str]], dict[str, Any] | None]


def _get_products(state: dict[str, Any], acct: dict[str, str]) -> dict[str, Any]:
    return {
        "adcp_version": ADCP_VERSION,
        "buying_mode": "wholesale",
        "brand": {"domain": "testbrand.example"},
        "account": acct,
    }


def _get_products_brief(state: dict[str, Any], acct: dict[str, str]) -> dict[str, Any]:
    # Guaranteed / curated products only surface on the brief path; the
    # fixture product is one of them, so this is the call that feeds the
    # creative and media-buy steps with a product and format option.
    return {
        "adcp_version": ADCP_VERSION,
        "buying_mode": "brief",
        "brief": "display banners for a gaming brand",
        "brand": {"domain": "testbrand.example"},
        "account": acct,
    }


def _sync_accounts(state: dict[str, Any], acct: dict[str, str]) -> dict[str, Any]:
    return {
        "adcp_version": ADCP_VERSION,
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


def _sync_creatives(state: dict[str, Any], acct: dict[str, str]) -> dict[str, Any] | None:
    if "option_ref" not in state:
        return None
    state["creative_id"] = f"sweep_cr_{uuid.uuid4().hex[:8]}"
    return {
        "adcp_version": ADCP_VERSION,
        "idempotency_key": uuid.uuid4().hex,
        "account": acct,
        "creatives": [
            {
                "creative_id": state["creative_id"],
                "name": "Sweep banner",
                "format_kind": state["format_kind"],
                "format_option_ref": state["option_ref"],
                "assets": {"banner_image": _asset()},
            }
        ],
    }


def _list_creatives(state: dict[str, Any], acct: dict[str, str]) -> dict[str, Any]:
    return {"adcp_version": ADCP_VERSION, "account": acct, "pagination": {"max_results": 5}}


def _create_media_buy(state: dict[str, Any], acct: dict[str, str]) -> dict[str, Any] | None:
    if "option_ref" not in state:
        return None
    start, end = _window()
    state["start"], state["end"] = start, end
    return {
        "adcp_version": ADCP_VERSION,
        "idempotency_key": uuid.uuid4().hex,
        "account": acct,
        "brand": {"domain": "testbrand.example"},
        "start_time": start,
        "end_time": end,
        "packages": [
            {
                "product_id": state["product_id"],
                "pricing_option_id": "cpm_usd_fixed",
                "budget": 5000.0,
                "format_option_refs": [state["option_ref"]],
                "creatives": [
                    {
                        "creative_id": f"sweep_inline_{uuid.uuid4().hex[:8]}",
                        "name": "Sweep inline",
                        "format_kind": state["format_kind"],
                        "format_option_ref": state["option_ref"],
                        "assets": {"banner_image": _asset()},
                    }
                ],
            }
        ],
    }


def _needs_media_buy(extra: Callable[[dict[str, Any]], dict[str, Any]]) -> Builder:
    def build(state: dict[str, Any], acct: dict[str, str]) -> dict[str, Any] | None:
        if "media_buy_id" not in state:
            return None
        return {"adcp_version": ADCP_VERSION, "account": acct, **extra(state)}

    return build


# (label, tool name, builder). Labels are unique; the same tool may appear
# more than once (e.g. both get_products surfaces).
STEPS: list[tuple[str, str, Builder]] = []
PAYLOADS: dict[str, Builder] = {
    "get_adcp_capabilities": lambda state, acct: {},
    "list_creative_formats": lambda state, acct: {"adcp_version": ADCP_VERSION},
    "list_accounts": lambda state, acct: {"adcp_version": ADCP_VERSION},
    "sync_accounts": _sync_accounts,
    "get_products": _get_products,
    "get_products (brief)": _get_products_brief,
    "sync_creatives": _sync_creatives,
    "list_creatives": _list_creatives,
    "create_media_buy": _create_media_buy,
    "get_media_buys": _needs_media_buy(lambda s: {"media_buy_ids": [s["media_buy_id"]]}),
    "update_media_buy": _needs_media_buy(
        lambda s: {"idempotency_key": uuid.uuid4().hex, "media_buy_id": s["media_buy_id"], "paused": True}
    ),
    "get_media_buy_delivery": _needs_media_buy(lambda s: {"media_buy_ids": [s["media_buy_id"]]}),
    "provide_performance_feedback": _needs_media_buy(
        lambda s: {
            "idempotency_key": uuid.uuid4().hex,
            "media_buy_id": s["media_buy_id"],
            "measurement_period": {"start": s["start"], "end": s["end"]},
            "performance_index": 1.1,
        }
    ),
    "get_signals": lambda state, acct: {
        "adcp_version": ADCP_VERSION,
        "discovery_mode": "brief",
        "signal_spec": "sports fans",
        "account": acct,
    },
}

# Capture ids from results for later calls.
CAPTURE: dict[str, Callable[[dict[str, Any], dict[str, Any]], None]] = {}


def _capture_products(state: dict[str, Any], sc: dict[str, Any]) -> None:
    for product in sc.get("products") or []:
        options = product.get("format_options") or []
        if options:
            state["product_id"] = product["product_id"]
            option = options[0]
            state["format_kind"] = option["format_kind"]
            state["option_ref"] = {
                "scope": "product",
                "product_id": product["product_id"],
                "format_option_id": option["format_option_id"],
            }
            return


def _capture_media_buy(state: dict[str, Any], sc: dict[str, Any]) -> None:
    if sc.get("media_buy_id"):
        state["media_buy_id"] = sc["media_buy_id"]


CAPTURE["get_products"] = _capture_products
CAPTURE["get_products (brief)"] = _capture_products
CAPTURE["create_media_buy"] = _capture_media_buy


def _tool_name(label: str) -> str:
    """Map a step label (``"get_products (brief)"``) to its MCP tool name."""
    return label.split(" ")[0]


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def _summarise(sc: dict[str, Any] | None) -> str:
    if not sc:
        return ""
    keys = [
        k
        for k in ("status", "media_buy_id", "products", "creatives", "accounts", "formats", "media_buys", "signals")
        if k in sc
    ]
    parts = []
    for k in keys:
        v = sc[k]
        parts.append(f"{k}={len(v)}" if isinstance(v, list) else f"{k}={v}")
    return " ".join(parts)[:90]


@pytest.mark.requires_db
def test_every_advertised_mcp_tool_responds_and_validates(authenticated_principal):
    p = authenticated_principal
    acct = {"account_id": p["account_id"]}
    rows: list[tuple[str, str, str]] = []
    state: dict[str, Any] = {}

    def factory(app):
        def httpx_factory(**hk):
            hk.setdefault("timeout", 60.0)
            hk["transport"] = httpx.ASGITransport(app=app)
            hk["base_url"] = "http://localhost"
            return httpx.AsyncClient(**hk)

        transport = StreamableHttpTransport(
            url="http://localhost/mcp/",
            headers={"x-adcp-auth": p["access_token"], "x-adcp-tenant": p["tenant_id"]},
            httpx_client_factory=httpx_factory,
        )

        async def run() -> None:
            async with Client(transport) as client:
                tools = {t.name: t for t in await client.list_tools()}
                missing_schema = sorted(n for n, t in tools.items() if not t.outputSchema)
                rows.append(
                    (
                        "tools/list",
                        "PASS" if tools and not missing_schema else "FAIL",
                        f"{len(tools)} tools; without outputSchema: {missing_schema or 'none'}",
                    )
                )
                covered = {_tool_name(label) for label in PAYLOADS}
                ordered = [lbl for lbl in PAYLOADS if _tool_name(lbl) in tools] + sorted(
                    n for n in tools if n not in covered
                )
                for label_name in ordered:
                    name = label_name
                    tool = _tool_name(name)
                    builder = PAYLOADS.get(name)
                    if builder is None:
                        args: dict[str, Any] | None = {}
                        label = "NO-PAYLOAD"
                    else:
                        args = builder(state, acct)
                        label = "PASS"
                    if args is None:
                        rows.append((name, "SKIP", "prerequisite id missing (earlier step failed)"))
                        continue
                    try:
                        result = await client.call_tool_mcp(tool, args)
                    except Exception as exc:  # noqa: BLE001 - report, keep sweeping
                        rows.append((name, "FAIL", f"transport error {type(exc).__name__}: {str(exc)[:110]}"))
                        continue
                    sc = result.structured_content or {}
                    if result.is_error:
                        err = (sc.get("adcp_error") or {}) if isinstance(sc, dict) else {}
                        rows.append(
                            (
                                name,
                                "FAIL" if label == "PASS" else label,
                                f"isError code={err.get('code')} {str(err.get('message') or sc)[:100]}",
                            )
                        )
                        continue
                    try:
                        jsonschema.validate(sc, tools[tool].outputSchema)
                        schema_note = "schema ok"
                    except jsonschema.ValidationError as exc:
                        label = "FAIL"
                        schema_note = f"outputSchema violation: {exc.message[:90]}"
                    if name in CAPTURE and isinstance(sc, dict):
                        CAPTURE[name](state, sc)
                    rows.append((name, label, f"{schema_note}; {_summarise(sc)}"))

        return run()

    run_on_app_loop(factory)

    table = "\n".join(f"{tool:32} | {outcome:10} | {note}" for tool, outcome, note in rows)
    print("\nMCP capability sweep\n" + table)
    bad = [r for r in rows if r[1] != "PASS"]
    assert not bad, f"{len(bad)} MCP tools did not pass:\n{table}"
