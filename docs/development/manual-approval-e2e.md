# Manual-approval end-to-end testing

How to exercise the full media-buy lifecycle that needs a human in the loop:
a buyer creates a media buy with creatives, an operator approves the
creatives and the buy in the Admin UI, the ad server receives the order, and
the buyer sees the outcome. Two tools cover it:

| Tool | Runs where | Ad server | Approval |
|------|------------|-----------|----------|
| `tests/integration/test_manual_approval_end_to_end.py` | In-process, local Postgres | Mock adapter | Automated through the Flask admin routes |
| `scripts/manual_approval_e2e.py` | Against a running sales agent (dev, local stack) | Whatever the tenant is configured with (GAM, Improve Digital, …) | A human, in the Admin UI; the script waits |

Jira: [FMS-2029](https://azerion-advertising.atlassian.net/browse/FMS-2029).

## What the flow asserts

1. `create_media_buy` on a tenant with *Human review required* returns a
   **synchronous success envelope**: `status: completed`,
   `media_buy_status: pending_start` (or `pending_creatives` when no
   creatives were sent) and a `media_buy_id`. It is not a `submitted` task;
   adcp 8 rejects hand-built submitted envelopes, so this shape is the contract.
2. The buy is persisted as `pending_approval` with a `requires_approval`
   workflow step; inline creatives are stored as `pending_review`.
3. After the operator approves the creative and then the workflow step, the
   adapter order is created and the buy leaves `pending_approval`
   (`scheduled`/`active` on the mock adapter, `pending_ad_server_approval` on
   GAM until the network approves the order).
4. A buyer that registered `push_notification_config` receives the terminal
   `create_media_buy` task webhook (A2A `Task`, or an MCP payload echoing the
   buyer's `operation_id`).
5. `list_creatives` reports the creative `approved`. This is the buyer-visible
   signal of approval: the wire `get_media_buys.status` stays `pending_start`
   both before and after approval because the flight has not started yet.

## MCP capability sweep (local, automated)

`tests/integration/test_mcp_capability_sweep.py` calls every tool the server
advertises over MCP with the real `fastmcp` client: `tools/list` (each tool
must carry an `outputSchema`), then one minimal valid call per tool in
dependency order (products → creatives → media buy → update, delivery,
feedback), validating each structured result against the advertised
`outputSchema` with `jsonschema`. A tool with no payload defined is still
called with `{}` and reported as `NO-PAYLOAD`, so new tools cannot escape
coverage. Run with `-s` to see the per-tool table:

```bash
scripts/run-test.sh tests/integration/test_mcp_capability_sweep.py -x -s
```

## Running the integration test (local, automated)

Needs Docker for the throwaway Postgres; nothing else.

```bash
scripts/run-test.sh tests/integration/test_manual_approval_end_to_end.py -x -v
# later
.claude/skills/agent-db/agent-db.sh down
```

`scripts/run-test.sh` starts the per-worktree Postgres, exports
`DATABASE_URL`, and runs pytest. The test provisions its own tenant
(`human_review_required=True`) through `provision_wire_principal(...)` in
`tests/integration/test_delegate_wire_envelope_cross_transport.py`, drives the
buyer side with the real `ADCPClient` over A2A, receives the webhook on a
loopback HTTP server, and approves through the Flask admin routes using the
`authenticated_admin_client` fixture. It passes with the project's standard
`uv sync --extra dev` environment.

## Running against a deployment (dev, manual approval)

The script is buyer-side only. It creates the buy, prints where to approve,
then polls `list_creatives` and `get_media_buys` until the approval is
visible or the wait expires.

### Prerequisites

- A **buyer token for the target tenant**: Admin UI → Advertisers → API
  Token. Tokens are tenant-scoped; a token from another tenant is accepted by
  the host but acts on *its own* tenant's account and products.
- *Human review required* enabled on the tenant, otherwise the buy
  auto-approves and the script says so.
- For GAM tenants: the GAM daily-spend guard caps each package at
  10 EUR/day, so keep `budget / days` below that.
- For a deployment still on adcp 7 (spec 3.1): pass `--adcp-version 3.1`.
  adcp 8 clients pin 3.2 by default and a 3.1 seller rejects them. The
  shipped scenarios set this already.

### Named scenarios

`SCENARIOS` at the top of `scripts/manual_approval_e2e.py` holds the
tenant/product variations. Any explicit flag overrides the scenario value.

| Scenario | Tenant | Ad server | Product |
|----------|--------|-----------|---------|
| `azerion-gaming-gam-display` | azerion-gaming (dev) | Google Ad Manager | `prod_ea17a482`, 2-day 2 EUR display for azerion.com, 300×250 banner, click to azerion.com |
| `viva-gaming-improve-display` | viva-gaming (dev) | Improve Digital | `prod_1f86ac53`, same campaign shape |

```bash
# 1. read-only check: connectivity, account, product/format/pricing selection
uv run python scripts/manual_approval_e2e.py --scenario azerion-gaming-gam-display \
    --token "$DEV_SALES_AGENT_TOKEN" --dry-run

# 2. create the buy and wait for the operator (prints the Admin UI links)
uv run python scripts/manual_approval_e2e.py --scenario azerion-gaming-gam-display \
    --token "$DEV_SALES_AGENT_TOKEN" --wait-minutes 30

# 3. if the wait expired before approval, poll the same buy again
uv run python scripts/manual_approval_e2e.py --scenario azerion-gaming-gam-display \
    --token "$DEV_SALES_AGENT_TOKEN" \
    --resume-media-buy-id mb_xxx --resume-creative-id e2e_inline_xxx --wait-minutes 5
```

While it polls, approve in the Admin UI in this order:

1. Creatives → review → approve the printed creative id.
2. Workflows → approve the `create_media_buy` step for the printed media buy.
   This is the step that creates the order in the ad server.

Exit codes: `0` approval observed, `1` setup/create failure, `2` timed out
waiting for approval.

### Useful flags

| Flag | Purpose |
|------|---------|
| `--protocol a2a|mcp` | Transport (A2A at `/`, MCP at `/mcp/`) |
| `--product-id` | Required product; the script looks on both the wholesale and brief surfaces and fails rather than substituting another product |
| `--pricing-option-id`, `--bid-price` | Fixed pricing is preferred; auction pricing gets a `bid_price` (default `max(floor, guidance, 1.0)`) |
| `--budget`, `--days`, `--brand-domain`, `--brief` | Campaign shape |
| `--asset-url`, `--width`, `--height`, `--click-url` | The inline creative; the format option is chosen to match width/height |
| `--webhook-url`, `--webhook-secret` | Register a push config (public HTTPS receiver; HMAC auth) |
| `--account-id` | Account to buy under (default: first `list_accounts` result) |
| `--adcp-version` | Wire version pin |

### Adding a scenario

Add a row to `SCENARIOS` with `base_url`, `tenant`, `adserver`,
`product_id`, `brand_domain`, `brief`, `budget`, `days`, asset fields,
`click_url` and `adcp_version`. Run it once with `--dry-run` to confirm the
product, format option and pricing selection before creating anything.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `AdCP version '3.2' is not supported by this server` | Seller runs adcp 7; add `--adcp-version 3.1` |
| `Package requires bid_price for auction-based cpm pricing` | Auction product; the script now sends `bid_price`, or pass `--bid-price` |
| `Package daily budget … exceeds maximum daily spend per package (10.00 EUR)` | GAM guard; lower `--budget` or raise `--days` |
| `product 'prod_x' not offered to this account/brand` | Wrong tenant token, or the brand is not authorised for that product |
| Buy auto-approved (no pending state) | *Human review required* is off on the tenant |
| `list_accounts` shows an unexpected account | The token belongs to another tenant |
| Test run ends with `Task was destroyed but it is pending!` | Fixed: the harness now shuts its loop down from `pytest_sessionfinish`; if it reappears, check `tests/harness/_asgi_app.py` |

## Known gaps

- The in-process test covers the mock adapter only. GAM and Improve Digital
  are exercised through the script against a configured tenant; an
  in-process GAM variant would need the adapter's `dry_run` mode and is not
  built yet.
- The management API `POST /tenants/<id>/workflows/<id>/approve` only records
  the decision; it does not create the adapter order or send the buyer
  webhook. Use the Admin UI (or the Flask route) for a real approval.
- For 3.0-pinned MCP buyers, adcp 8 rewrites `media_buy_status` into
  `status` but its advertised `outputSchema` is not widened for the 3.2
  `allOf` shape, so schema-validating MCP clients reject the response. This is
  an SDK defect; A2A and 3.1+ buyers are unaffected.
