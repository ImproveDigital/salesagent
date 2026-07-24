# Improve Digital Adapter — Integration Plan

Goal: add an `improvedigital` ad-server adapter with **full parity with GAM across every
MCP/A2A tool** — media buy creation, update, delivery metrics, inventory, syncing,
creatives, and performance feedback.

| | |
|---|---|
| **Canonical adapter key** | `improvedigital` |
| **Platform** | Improve Digital 360 Polaris / 360Yield SSP curation marketplace (Azerion) |
| **Primary API doc** | [Marketplace API V3 (Confluence)](https://azerion-advertising.atlassian.net/wiki/spaces/imp/pages/828964865/Marketplace+API+V3) ✅ read (PDF export) |
| **Auth doc** | [Client Authentication (Confluence)](https://azerion-advertising.atlassian.net/wiki/spaces/imp/pages/459317/Client+Authentication) ✅ read |
| **Reporting doc** | [Improve Marketplace Report API (Confluence)](https://azerion-advertising.atlassian.net/wiki/spaces/imp/pages/828997633/Improve+Marketplace+Report+API) ✅ read |
| **API base URL** | `https://api.360yield.com` — v3 endpoints prefixed `/rtb/v3/`; Report API at `/report`; OAuth at `/oauth/token` |
| **OpenAPI spec** | <https://openapi.360yield.com/dashboard> (behind login — grab JSON export during Phase 0 probe) |
| **Auth** | OAuth2 `client_credentials` → short-lived bearer token, **no refresh token** (re-mint on expiry/401); optional `DELETE /oauth/logout/{token}` |
| **Reference implementation** | FreeWheel adapter (`src/adapters/freewheel/`) — the modern, clean template. Borrow GAM's manager decomposition only if needed. |
| **Playbook** | `docs/adapters/adding-a-new-adapter.md` |

---

## Marketplace API V3 — key facts (from the docs)

### Auth (`Client Authentication` page)   

- `POST https://api.360yield.com/oauth/token`, body `grant_type=client_credentials`,
  credentials via `Authorization: Basic base64(client_id:client_secret)` (or form data).
- Response: `{"value": "<token>", "expiresIn": <seconds>, "tokenType": "bearer", "refreshToken": null, ...}`
  — example lifetime was ~11 minutes. **No refresh token**: on expiry the API returns
  401 and the client must mint a new token (exactly the FW `_transport.py`
  refresh-on-401 pattern).
- All other calls: `Authorization: Bearer <token>`, `Content-Type/Accept: application/json`.

### Domain model & conventions

- **Campaign** = top-level container: buyer, currency, flight dates, budget.
  **Line Item** = execution unit: bid price, targeting, inventory selection, frequency
  capping, delivery goal (`BUDGET` | `IMPRESSION`).
- **Unified Deal (v3)** = campaign + line item created **atomically in one call** — the
  recommended creation flow. This maps 1 MediaBuy+Package pair per call.
- **Two buying types**: `Universal Deal (UDID)` — RTB deal-based, buyer bids via a 3rd
  party DSP (the primary v3 flow); `Classic (Direct)` — direct campaigns with creatives
  hosted/managed by the Improve adserver.
- Pagination: `offset`/`limit`/`sort` + response metadata
  (`totalNumberOfElemements`, `contentRange`). Filtering via `params` query map.
- Errors: `ExceptionWrapper` JSON (`type`, `messages[].errorCode/property/description`);
  400 validation, 401 token expired, 403 insufficient permissions.
- Dates ISO 8601. Currencies: EUR, USD, GBP, DKK, SEK, AUD, CZK, HUF, CHF, NOK, SGD,
  HKD, MYR, CAD, TRY. Media types: `BANNER, VIDEO, NATIVE, AUDIO, NATIVE_DISPLAY, NATIVE_VIDEO`.

### Endpoints by adapter concern

**Media buy lifecycle**
- Create: `POST /rtb/v3/unified-deals` (`UnifiedDealDto`: required `name`, `start_date`;
  plus `end_date`, `budget`, `cpm_bid`, `impression_cap`, `media_type`,
  `pricing_model_type`, `frequency_interval(_type)`, `buying_entity_id` (DSP),
  `buying_entity_office_ids` (buyer seats), `metadata` (advertiser/agency/buyer IDs),
  all targeting fields, `placement_ids`/`excluded_placement_ids`/`package_ids`).
- Read: `GET /rtb/v3/unified-deals/{dealId}`; list `GET /rtb/v3/campaigns`,
  `GET /rtb/v3/line-items` (fields incl. `status`, `line_item_state`, `active`).
- Update: `PUT /rtb/v3/unified-deals/{dealId}` — **must send `update_mask=<fields>`**
  (e.g. `update_mask=budget,name`); same fields as create, all optional.
- Pause/resume: `PUT /rtb/v3/campaigns/{campaignId}/line-items/{lineItemId}/status?active={boolean}`.
- Archive (soft delete): `PUT /rtb/v3/campaigns/{campaignId}/archive`,
  `.../line-items/{lineItemId}/archive`, `.../creatives/{creativeId}/archive`.
  Hard delete: `DELETE /rtb/v3/unified-deals/{dealId}`.
- Config audit: `GET .../line-items/{lineItemId}/applied-targetings`.
- JSON Schemas for payload validation: `GET /schema/rtb/v3/unified-deal-creation` / `unified-deal-update`.

**Targeting** (consolidated GET|PUT under `/rtb/v3/targeting/campaigns/{campaignId}/line-items/{lineItemId}/`)
- `/time` (day/hour), `/segments` (brand safety, DV, Captify, Azerion Intelligence, DMP
  audience, pixel, IAB topics, SDA), `/placement` (platform/media type/sizes, video
  format, playback, CTV content objects), `/location` (region/country/state/city +
  up to 10 IP ranges — **no postal codes**), `/environment` (language, ISP/carrier,
  device types, OS, browser, device models), `/context` (domains/bundles incl. global
  lists, URL substrings, key-values).
- Per-line-item extras: `size-targeting`, `isp-targeting`, `url-substring-targeting`, `pg-tag`.
- Lookup endpoints for every dimension under `/common/v1|v2/*` and `/rtb/v1|v2/*`
  (countries, regions, geo search, device types/OS/browsers, ISPs, sizes, languages,
  IAB topics, video format types, DMP segments, buying types, DSPs + seats,
  advertisers, agencies, sales persons).

**Inventory (buy-side selection)**
- Placement search: `GET /rtb/v3/placements` — filters: `publisher_ids`, `size_ids`,
  `iab_categories`, `placement_types`, `video_format_types`, `allow_pg_deals`,
  `azerion_owned`, `seller_types` (`PUBLISHER|INTERMEDIARY|BOTH`), `tier_ids`,
  free-text `search`, paginated.
- Packages (reusable placement groupings, with lock feature):
  `GET /rtb/v3/line-items/{lineItemId}/packages`, `POST /rtb/v3/automatic-packages`
  (auto-populate from a forecasting query).
- Line-item placement assignment: `GET /rtb/v3/line-item-placements/{lineItemId}/search`,
  `POST .../load`, `PUT .../update|assign-all|unassign-all|complete|clean-up`.
- All placements dump: `GET /rtb/v1/placements`. Publishers per line item:
  `GET /rtb/v3/line-items/{id}/targeted-publishers`.

**Creatives (Classic campaigns only)**
- List: `GET /rtb/v3/classic/campaigns/{campaignId}/creatives` (with
  `assigned_line_items` + per-creative impression counts); export `.../creatives-export`.
- VAST validation: `GET /rtb/v3/classic/creatives/validate-vast-url?url={vastUrl}`.
- Archive: `PUT /rtb/v3/campaigns/{campaignId}/creatives/{creativeId}/archive`.
- ⚠️ **No creative create/upload endpoint documented in v3** — see gap G1.

**Delivery / reporting**
- Real-time trend: `GET /rtb/v3/line-items/{lineItemId}/impression-delivery` — last 6h
  from Graphite, ~15 min delay, explicitly **not a source of truth**.
- Definitive: **Report API** at `https://api.360yield.com/report`:
  - `POST /report/ext/generation` — async job, `report_type: "EXT_CONSOLIDATE"`,
    CSV/Excel, ≤1M rows; body: `date_range` (quick/relative/fixed), `dimensions`
    (≤20: `campaign_id`, `line_item_id`, `placement_id`, `day`, `date_hour`, …),
    `metrics` (`impressions`, `clicks`, `advertiser_payout` = spend, `ecpm_mkt`,
    video events incl. `complete`/quartiles, viewability), `filters`
    (`ColumnOperationValueTriplet`, e.g. `campaign_id IN [...]`), `currency_id` (1=EUR,
    2=USD, …), `timezone`, optional `scheduling` (email delivery).
  - `GET /report/ext/generation-status/{report_generation_id}` — `ENQUEUED → GENERATING
    → UPLOADED…`, `report_download_url` when done.
  - `POST /report/ext/preview` — **synchronous, ≤500 rows returned as JSON** — ideal
    for `run_reporting_sync` at our scale (per-tenant campaign/line-item rollups fit
    easily in 500 rows; fall back to the async CSV job above if a tenant outgrows it).
  - `POST /report/allowed-filters` — dimension/filter discovery.

### Confirmed entity mapping

| AdCP entity | GAM | Improve Digital (**confirmed**) |
|---|---|---|
| `MediaBuy` | Order | **Campaign** (created via Unified Deal) |
| `Package` | LineItem | **Line Item** (Unified Deal = campaign+LI atomically; extra packages = extra line items on the campaign) |
| `Creative` | Creative + LICA | Classic-campaign creative (list/archive only in v3 — see G1); UDID deals: creatives live in the buyer's DSP |
| Inventory | AdUnit/Placement tree | **Placements** (buy-side search) + **Packages**; taxonomy: publisher → site → placement (+ sizes, placement types, tiers) |
| Delivery metrics | ReportService | **Report API** `EXT_CONSOLIDATE` (+ real-time `impression-delivery` for trends) |
| Advertiser | Company | `metadata.advertiser_uuid` / `/api/metadata-advertisers` lookup; buyer = `marketplace_buyer` / `buying_entity` (DSP + seat) |
| Targeting | GAM targeting tree | 6 consolidated targeting DTOs per line item |

---

## Blockers & open gaps

- [x] ~~**B1 — API doc access.**~~ **Resolved** — all three Confluence pages exported to
      PDF and read (Marketplace API V3, Client Authentication, Report API). Text
      extracts worth committing to `docs/adapters/improvedigital/api-doc/` for future
      sessions.
- [ ] **B2 — API credentials.** Need an OAuth2 client ID + secret ("issued to each
      customer, one for each client application" — request via the platform team) with
      roles covering: unified-deal read/write, placement search, Report API
      (needs "an active Polaris user with the necessary permissions"). Confirm whether
      a staging/sandbox environment exists. Also grab the OpenAPI JSON from
      <https://openapi.360yield.com/dashboard> once we have a login.
- [ ] **G1 — Creative upload has no documented v3 endpoint.** v3 only lists, exports,
      VAST-validates, and archives creatives on Classic campaigns; creation happens
      "from within the line item form" (UI) or presumably via v1/v2 endpoints not in
      this doc. **Decision**: start with **UDID deals** (primary v3 flow) where
      creatives are managed buyer-side in the DSP — `add_creative_assets` /
      `associate_creatives` then raise a clear `FeatureNotSupportedException` (No Quiet
      Failures) or route to a HITL manual-approval step. Ask the Improve team whether a
      programmatic Classic creative-upload API exists; if yes, wire it in a follow-up.
- [ ] **G2 — `pricing_model_type` allowed values not enumerated** in the doc (plain
      "string"). Probe `GET /common/v2/buying-types` + the unified-deal JSON schema
      (`GET /schema/rtb/v3/unified-deal-creation`) and confirm with the platform team.
      Working assumption: CPM (deals are `cpm_bid`-driven; CPC exists — "BUDGET is
      default for CPC line items").
- [ ] **G3 — UDID vs Classic strategy.** UDID requires a `buying_entity_id` (DSP) +
      seat — i.e. the buyer transacts through their own DSP and our "media buy" is the
      deal setup. Classic means Improve adserver serves our creatives directly. This
      changes what `create_media_buy` needs from the buyer principal (DSP + seat IDs
      as principal-level platform mappings, like GAM's `advertiser_id`). Decide with
      product which flow (or both) AdCP buyers get. **Recommendation: UDID first.**

---

## What we have vs. what we need

### Already in the codebase (nothing to build)

- **Adapter framework** — `src/adapters/base.py`: `AdServerAdapter` ABC,
  `AdapterCapabilities`, `TargetingCapabilities`, `BaseConnectionConfig` /
  `BaseProductConfig`, sync-result plumbing (`AdapterSyncResult` → `sync_jobs` table),
  `DeliveryDataUnavailable`, and reusable helpers (`_build_package_responses`,
  `_aggregate_stat_rows_to_delivery_response`, `_empty_delivery_response`,
  `_wrap_sync_run`, `_resolve_pricing_rate`).
- **Tool layer is adapter-agnostic** — all 10 wire tools route
  `core/platforms/_delegate.py` → `src/core/tools/*_impl` → `get_adapter()`. No tool
  changes needed; we only implement the adapter interface and register it.
- **Shared background schedulers** — `src/services/adapter_sync_scheduler.py` +
  `adapter_sync_orchestration.py` run `run_inventory_sync` / `run_reporting_sync`
  automatically based on capability flags. `delivery_webhook_scheduler.py` polls
  `get_media_buy_delivery` for webhooks. No new scheduler needed.
- **Generic adapter persistence path** — FreeWheel-style `connection_config_class`
  persisted through `config_json` (no new `AdapterConfig` columns; do NOT copy GAM's
  legacy per-column approach).
- **Admin UI framework** — picker card + per-adapter `connection_config.html` /
  `product_config.html` convention, generic capabilities endpoint in
  `src/admin/blueprints/adapters.py`, `/admin/scheduling` freshness page.
- **HITL workflow support** — manual-approval steps for anything the API can't do
  (GAM `workflow.py` precedent, `manual_approval_required` on connection config).

### To build (the adapter itself)

Everything under "Phases" below: the `src/adapters/improvedigital/` package, two DB
cache tables + repositories + migrations, registration in 3 places, two UI templates +
picker card, 3 admin endpoints, tests, docs, regenerated OpenAPI.

### Required adapter surface for full MCP-tool parity (tool-layer trace × API doc)

| MCP/A2A tool | Adapter methods it needs | Improve Digital implementation |
|---|---|---|
| `get_products` | `get_supported_pricing_models`, `default_channels` | CPM (+CPC?) — pending G2; channels: display, olv, audio, native (from media types) |
| `create_media_buy` | `validate_media_buy_request`, `create_media_buy`, `add_creative_assets`, `associate_creatives` | `POST /rtb/v3/unified-deals` per package (+ placement/package assignment); creatives: G1 (UDID → not-supported/HITL) |
| `update_media_buy` | `update_media_buy` — actions `pause_media_buy`, `resume_media_buy`, `pause_package`, `resume_package`, `update_package_budget`, generic `update` | pause/resume → `PUT .../line-items/{id}/status?active=false|true` (campaign-level = all its line items); budget/dates → `PUT /rtb/v3/unified-deals/{id}` with `update_mask=` |
| `get_media_buys` (list) | `get_packages_snapshot` (gated by `supports_realtime_reporting`) | Reporting cache (Phase 3); optionally freshen from `GET /rtb/v3/line-items/{id}/impression-delivery` |
| `get_media_buy_delivery` | `get_media_buy_delivery` | Aggregate reporting cache (fed by Report API) |
| `sync_creatives` | none (DB-only) | — |
| `list_creatives` / `list_creative_formats` | none (DB/config-driven) | `formats.py` declares formats from media types + `GET /rtb/v1/sizes-all` |
| `provide_performance_feedback` | `update_media_buy_performance_index` | No platform equivalent → store locally (GAM pattern) |
| `get_signals` | none (operator config) | Future: DMP segments (`/dmp/v2/dmp-segments`) + contextual segments as signals |
| Background jobs | `run_inventory_sync`, `run_reporting_sync`, `latest_*_sync_at` + capability flags | Placement search sweep; Report API preview/generation |
| Ops/UI | `check_permissions`, `get_available_inventory`, `check_media_buy_status`, `get_targeting_capabilities`, `get_creative_formats`, `validate_product_config` | token mint + probe reads; placement cache; `GET /rtb/v3/unified-deals/{id}` status |

---

## Phase 0 — Discovery ⚠️ gate for implementation

- [x] Obtain + read the Marketplace API V3 doc, Client Authentication, Report API docs.
- [x] Record REST style, pagination, token flow, error format, entity mapping (see
      "Key facts" above).
- [ ] Resolve **B2**: get client credentials (+ Polaris user for Report API), confirm
      sandbox story, download the OpenAPI JSON from openapi.360yield.com.
- [ ] Probe script (scratchpad) against every endpoint above — classify `200`/`403`/`404`
      per entity (FW pattern). Especially: unified-deal create/update/delete,
      placement search, Report API preview.
- [ ] Resolve **G2** (pricing models) via `GET /common/v2/buying-types` + JSON schema +
      platform team.
- [ ] Resolve **G1/G3** (creatives; UDID vs Classic) with product + Improve team.
- [ ] Get the entity mapping sanity-checked by the Improve Digital platform team.
- [ ] Confirm targeting dimensions to expose in v1 of `targeting.py` (geo country/
      region/city ✅, device ✅, day/hour ✅, domain/bundle ✅, key-value ✅; **no postal
      codes** → `TargetingCapabilities(postal_supported=False)` and hard-reject).
- [ ] Document every scope blocker in `docs/adapters/improvedigital/README.md`
      ("Scope grants still needed"), tiered by what they unblock.
- [ ] Save probe findings as `.claude/research/improvedigital-api.md`; commit doc
      text extracts under `docs/adapters/improvedigital/api-doc/`.

## Phase 1 — Adapter package scaffolding (`src/adapters/improvedigital/`)

- [ ] `__init__.py` — export `ImproveDigitalAdapter`, `ImproveDigitalConnectionConfig`,
      `ImproveDigitalProductConfig`, error classes.
- [ ] `_transport.py` — OAuth2 client-credentials transport:
      `POST /oauth/token` with Basic auth, cache token until `expiresIn`, **re-mint on
      401** (no refresh token), retry/backoff, JSON handling, `DELETE
      /oauth/logout/{token}` on close (best-effort), `ExceptionWrapper` → typed errors.
- [ ] `schemas.py` — `ImproveDigitalConnectionConfig(BaseConnectionConfig)`:
      `client_id`, `client_secret` (Fernet-encrypted via `@field_serializer`, FW
      pattern), optional `api_base_url` override, default `buying_entity_id` +
      `buying_entity_office_ids` (UDID), default `currency`, `timezone`.
      `ImproveDigitalProductConfig(BaseProductConfig)`: `placement_ids`,
      `package_ids`, `size_ids`, `media_type`, placement-search filter defaults
      (`azerion_owned`, `seller_types`, `iab_categories`), pricing defaults,
      targeting template.
- [ ] `client.py` — facade composing sub-clients: `deals` (unified deals, campaigns,
      line items, status/archive), `inventory` (placement search, packages, line-item
      placements), `lookups` (sizes, geo, devices, DSPs/seats, advertisers),
      `reporting` (Report API generation/status/preview), `creatives` (list/VAST
      validate/archive — Classic only).
- [ ] `entities.py` — Pydantic/dataclass models (`UnifiedDealDto`,
      `CommonDealLineItemDto`, placement, report rows, pagination envelope).
- [ ] `adapter.py` — `ImproveDigitalAdapter(AdServerAdapter)`:
  - [ ] `adapter_name = "improvedigital"`, `default_channels = ["display", "olv", "audio", "native"]`,
        `default_delivery_measurement = {"provider": "improvedigital"}`
  - [ ] `capabilities = AdapterCapabilities(supports_inventory_sync=True,
        supports_reporting_sync=True, supports_realtime_reporting=True,
        supports_custom_targeting=True, supports_geo_targeting=True,
        inventory_entity_label="placement", supported_pricing_models=[...G2])`
  - [ ] `__init__` — resolve principal (buyer/DSP/seat mapping), connection config, auth
  - [ ] `validate_media_buy_request`, `create_media_buy` — one unified deal per
        package; assign placements/packages; apply targeting overlays
  - [ ] `update_media_buy` — **all 5 action strings** + generic update (`update_mask`!)
  - [ ] `add_creative_assets`, `associate_creatives`, `process_assets` — per G1:
        explicit `FeatureNotSupportedException` or HITL for UDID v1
  - [ ] `check_media_buy_status` — `GET /rtb/v3/unified-deals/{id}` → map
        `status`/`line_item_state`/`active`
  - [ ] `get_media_buy_delivery`, `get_packages_snapshot` (cache reads, Phase 3)
  - [ ] `update_media_buy_performance_index` (local store)
  - [ ] `get_supported_pricing_models`, `get_pricing_option_support`,
        `get_targeting_capabilities` (postal=False), `get_creative_formats`
  - [ ] `get_available_inventory` (async; from placement cache)
  - [ ] `check_permissions` — token mint + probe reads per scope
  - [ ] `validate_product_config`, `latest_inventory_sync_at`, `latest_reporting_sync_at`
- [ ] `targeting.py` — AdCP `Targeting` overlay → the 6 consolidated targeting DTOs
      (geo→`LocationTargetingDto`, device/os/browser→`EnvironmentTargetingDto`,
      dayparting→time, key-value/domains→`ContextTargetingDto`, media/size/video→
      placement DTO, audience/contextual→segments DTO). `validate_targeting()` rejects
      unsupported dims (postal!) explicitly.
- [ ] `formats.py` — `Format` declarations for BANNER (sizes from `/rtb/v1/sizes-all`),
      VIDEO (pre/mid/post-roll via video format types), AUDIO, NATIVE.

## Phase 2 — Inventory cache + sync

- [ ] Alembic migration `improvedigital_inventory`: PK `(tenant_id, entity_type,
      entity_id)`, `name`, `parent_id`, `raw_json` (JSONType), `last_synced_at`.
      Entity types: `publisher`, `placement`, `package`, `size`, `placement_type`.
      Check `uv run alembic heads` after rebase.
- [ ] ORM model `ImproveDigitalInventory` in `src/core/database/models.py`
      (cascade-delete on tenant).
- [ ] Repository `src/core/database/repositories/improvedigital_inventory.py` —
      tenant-scoped reads + `ON CONFLICT DO UPDATE` bulk upsert (all ORM access goes
      through it; enforced by `test_architecture_no_raw_select`).
- [ ] `inventory_sync.py` — paginated sweep of `GET /rtb/v3/placements` (+ sizes +
      placement types + packages lookups), return `AdapterSyncResult` per-entity
      counts + errors; wire as `run_inventory_sync` via `_wrap_sync_run` so the shared
      scheduler picks it up. Respect the doc's pagination metadata; JSON IDs → cast
      to the right type at the boundary.

## Phase 3 — Reporting cache + delivery read path

- [ ] Alembic migration `improvedigital_line_item_stats`: per line item `impressions`,
      `clicks`, `spend_micros` (from `advertiser_payout`, micros — not floats),
      `video_completions` (from `complete`), `currency`, `delivery_status`, `as_of`,
      `last_synced_at`.
- [ ] ORM model + repository with `get_by_line_item_ids`, `list_by_campaign`,
      `bulk_upsert`.
- [ ] `reporting_sync.py` — `run_reporting_sync`: `POST /report/ext/preview`
      (synchronous JSON, ≤500 rows) with dimensions `campaign_id, line_item_id`,
      metrics `impressions, clicks, advertiser_payout, complete`, filter
      `campaign_id IN (<active campaigns>)`, date range covering active flights;
      fall back to the async `POST /report/ext/generation` → poll
      `generation-status/{id}` → download CSV if a tenant exceeds 500 rows.
- [ ] `get_packages_snapshot()` — read cache, `None` for missing rows (never
      fabricate); map status → AdCP `DeliveryStatus` (6 values; **no `paused`** —
      inactive line item → `not_delivering`).
- [ ] `get_media_buy_delivery()` — aggregate cache rows via
      `_aggregate_stat_rows_to_delivery_response`; empty cache →
      `_empty_delivery_response`; raise `DeliveryDataUnavailable` only when data can't
      exist yet (webhook scheduler then skips instead of sending zeros).
- [ ] Optional (nice-to-have): blend `GET /rtb/v3/line-items/{id}/impression-delivery`
      into `get_packages_snapshot` for fresher trend data — but never as source of
      truth (the doc is explicit about this).

## Phase 4 — Registration (3 places — miss one and it's unreachable)

- [ ] `src/adapters/__init__.py` — `ADAPTER_REGISTRY["improvedigital"] = ImproveDigitalAdapter`.
- [ ] `src/admin/api_schemas/tenant_management.py` —
      `ImproveDigitalAdapterConfig(BaseModel)` with `type: Literal["improvedigital"]`,
      `SecretStr` for the client secret; add to the `AdapterConfig` discriminated union.
- [ ] `src/admin/tenant_management_api.py` — extend `_adapter_config_to_dict()` +
      `_persist_adapter_config()` (round-trip secrets through
      `ImproveDigitalConnectionConfig` so Fernet lands in `config_json` — the FW path,
      **not** GAM's legacy columns); add `_ADAPTER_CATALOG_METADATA` +
      `_ADAPTER_CONFIG_TYPED` entries.

## Phase 5 — Admin UI

- [ ] Picker card in `templates/tenant_settings.html` (copy the FreeWheel card block).
- [ ] `templates/adapters/improvedigital/connection_config.html` — client ID/secret,
      buying entity (DSP) + seat defaults, currency/timezone; Test Connection button +
      status div.
- [ ] `templates/adapters/improvedigital/product_config.html` — placement/package/size
      pickers with `data-entity-type`, populated from the inventory query endpoint;
      filters for `azerion_owned`/`seller_types`.
- [ ] All JS uses `const scriptRoot = '{{ request.script_root }}' || '';` — never
      hardcode paths.

## Phase 6 — Admin API endpoints (`src/admin/blueprints/adapters.py`)

- [ ] `POST /api/tenant/<tenant_id>/adapters/improvedigital/test-connection` —
      mint a token + probe read (e.g. `GET /rtb/v3/campaigns?limit=1`);
      `@require_tenant_access(role=("admin",), allow_embedded_writes=True)`;
      reject submitted ciphertext on secret fields.
- [ ] `GET /api/tenant/<tenant_id>/adapters/improvedigital/inventory` — read from the
      repository, keyed by `entity_type` query param.
- [ ] `POST /api/tenant/<tenant_id>/adapters/improvedigital/sync-inventory` —
      trigger sync, `role=("admin",)`.

## Phase 7 — Tests

- [ ] `tests/unit/test_improvedigital_schemas.py` — config round-trips; secret
      encryption (ciphertext serialization, round-trip, no double-encryption).
- [ ] `tests/unit/test_improvedigital_adapter.py` — registry wiring, dry-run,
      `__init__` cred validation, pricing models, targeting capabilities,
      `get_available_inventory` shape via mocked repository.
- [ ] `tests/unit/test_improvedigital_transport.py` — Basic-auth token mint, bearer
      header, retry, **re-mint-on-401** (no refresh token), token-expiry handling
      (mock at `requests.Session` level).
- [ ] `tests/unit/test_improvedigital_targeting.py` — every AdCP field → DTO mapping,
      every rejection path (postal!).
- [ ] `tests/unit/test_improvedigital_inventory_sync.py` — per-entity counts, error
      capture, idempotent upsert, pagination handling (mocked client).
- [ ] `tests/unit/test_improvedigital_reporting_cache.py` — empty-cache `None`/empty
      response, populated `Snapshot`/`DeliveryTotals`, status-enum mapping,
      preview→cache row translation (incl. `advertiser_payout` → micros).
- [ ] `tests/unit/test_improvedigital_update_media_buy.py` — all 5 action strings +
      `update_mask` correctness + `affected_packages` shape.
- [ ] `tests/integration/test_improvedigital_live.py` — `@pytest.mark.live`, gated on
      `IMPROVEDIGITAL_TEST_CLIENT_ID/SECRET`; token mint, placement search, report
      preview, full unified-deal create→check→pause→delete cycle.
- [ ] Extend `tests/unit/test_tenant_management_schemas.py` — happy path, rejection,
      discriminator routing through `ProvisionTenantRequest`.
- [ ] Extend `tests/integration/test_tenant_management_api_integration.py::test_list_adapters_returns_supported_catalog`.
- [ ] Respect quality rules: ≤10 mocks per file; AdCP compliance test if any
      client-facing schema changes (`tests/unit/test_adcp_contract.py`).

## Phase 8 — Documentation

- [ ] `docs/adapters/improvedigital/README.md` — entity mapping table, auth, capability
      matrix, live coverage matrix (✅/🟡/⏳/❌ per method), scope grants still needed
      (from probe), constraints (no postal targeting, creative upload gap G1, 500-row
      preview limit, 1M-row report cap).
- [ ] Commit API-doc text extracts under `docs/adapters/improvedigital/api-doc/`.
- [ ] Update `docs/adapters/README.md` — "Available Adapters" entry + "Choosing an
      Adapter" row.

## Phase 9 — Machine-readable specs

- [ ] `make openapi` after the tenant-management schema changes
      (`test_committed_openapi_json_matches_live_spec` fails otherwise).
- [ ] No adapter-specific OpenAPI artifacts.

## Phase 10 — Smoke + quality gates

- [ ] Apply migrations locally: `docker compose exec adcp-server python scripts/ops/migrate.py`.
- [ ] `make quality` green (format, lint, mypy, unit tests incl. structural guards).
- [ ] `tox -e integration` (repositories, sync, persistence all touch the DB layer).
- [ ] Live smoke vs the real platform: token mint → inventory sync → unified-deal
      create→status→pause→resume→budget-update→delete → report preview.
- [ ] Playwright check of the product-config UI: every picker populates from the
      synced cache.
- [ ] Restart the container after adding blueprint routes
      (`docker compose restart adcp-server`) — uvicorn caches imports.

## Phase 11 — Ship

- [ ] PR title: `feat(improvedigital): Improve Digital Marketplace API v3 adapter — auth, inventory sync, unified deals, reporting`.
- [ ] Commits reviewable in order: transport/auth → client → inventory sync → adapter
      core (unified deals) → targeting → reporting cache → registration/typed config →
      UI → tests/docs.
- [ ] PR body: live-coverage matrix + open gaps (G1 creatives, G2 pricing models).
- [ ] Unrelated improvements → separate issues.

---

## Suggested delivery milestones

1. **M0 — Discovery complete** (Phase 0 remainder): credentials, probe results, G1–G3
   decisions, OpenAPI JSON. *Implementation is gated on this.*
2. **M1 — Read path**: transport + client + inventory (placement) sync + cache + admin
   inventory endpoints + connection UI. Independently shippable; immediately useful
   for product configuration.
3. **M2 — Buy path**: unified-deal create/update/status, targeting translation,
   placement assignment.
4. **M3 — Delivery**: Report API sync + delivery read path + webhook-scheduler
   compatibility.
5. **M4 — Polish & ship**: registration, typed embedder config, tests, docs, OpenAPI,
   live smoke.

## Quick file checklist

| File | What |
|---|---|
| `src/adapters/improvedigital/__init__.py` | Public exports |
| `src/adapters/improvedigital/schemas.py` | ConnectionConfig + ProductConfig |
| `src/adapters/improvedigital/adapter.py` | `ImproveDigitalAdapter` |
| `src/adapters/improvedigital/client.py` | API client facade |
| `src/adapters/improvedigital/_transport.py` | OAuth2 transport (re-mint on 401) |
| `src/adapters/improvedigital/entities.py` | Upstream entity models |
| `src/adapters/improvedigital/targeting.py` | AdCP → 6 targeting DTOs |
| `src/adapters/improvedigital/formats.py` | Format declarations |
| `src/adapters/improvedigital/inventory_sync.py` | Placement/package sweep |
| `src/adapters/improvedigital/reporting_sync.py` | Report API preview/generation sync |
| `src/adapters/__init__.py` | `ADAPTER_REGISTRY` entry |
| `src/admin/api_schemas/tenant_management.py` | Typed config + union |
| `src/admin/tenant_management_api.py` | Persistence + catalog |
| `src/admin/blueprints/adapters.py` | test-connection / inventory / sync endpoints |
| `src/core/database/models.py` | 2 cache-table ORM models |
| `src/core/database/repositories/improvedigital_*.py` | 2 repositories |
| `alembic/versions/<rev>_improvedigital_*.py` | 2 migrations |
| `templates/tenant_settings.html` | Picker card |
| `templates/adapters/improvedigital/*.html` | Connection + product config forms |
| `tests/unit/test_improvedigital_*.py` | Unit tests (7 files) |
| `tests/integration/test_improvedigital_live.py` | Live smoke |
| `docs/adapters/improvedigital/README.md` | Adapter doc |
| `docs/adapters/improvedigital/api-doc/` | Committed API-doc extracts |
| `docs/api/tenant-management-openapi.{json,yaml}` | `make openapi` |

## Reference links

- [Marketplace API V3 (Confluence)](https://azerion-advertising.atlassian.net/wiki/spaces/imp/pages/828964865/Marketplace+API+V3)
- [Client Authentication (Confluence)](https://azerion-advertising.atlassian.net/wiki/spaces/imp/pages/459317/Client+Authentication)
- [Improve Marketplace Report API (Confluence)](https://azerion-advertising.atlassian.net/wiki/spaces/imp/pages/828997633/Improve+Marketplace+Report+API)
- [OpenAPI spec dashboard](https://openapi.360yield.com/dashboard) (login required)
- [azerion/improvedigital-publisher-mcp-server (public; supply-side Publisher API Swagger)](https://github.com/azerion/improvedigital-publisher-mcp-server)
- Internal playbook: `docs/adapters/adding-a-new-adapter.md`
- Reference adapter: `src/adapters/freewheel/` (PR #381)
- GAM manager decomposition (if needed at scale): `src/adapters/gam/managers/`
