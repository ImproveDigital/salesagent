# Improve Digital Adapter

Connects the Prebid Sales Agent to the **Improve Digital 360Yield
Marketplace API**, booking AdCP media buys as **Classic (direct) campaigns**
— campaigns whose creatives are hosted and served by the Improve adserver.

Registry key: `improvedigital`.

## Entity mapping

| AdCP concept | 360Yield entity | API surface |
|---|---|---|
| Media buy | Classic Campaign | `POST /rtb/v1/classic/campaigns` |
| Package | Classic Line Item | `POST /rtb/v1/classic/campaigns/{id}/line-items` |
| Creative | Classic Creative | `POST .../creatives` (multipart) / third-party-tag bulk upload |
| Delivery | Report API `EXT_CONSOLIDATE` | `POST /report/ext/preview` → local stats cache |
| Inventory | Placements / packages / sizes | `GET /rtb/v3/placements`, `/rtb/v1/packages`, `/rtb/v1/sizes-all` |

The adapter returns `improvedigital_<campaign_id>` as the buy reference; the
core layer persists it per package as `package_config["platform_order_id"]`
alongside the per-package `platform_line_item_id`.

## Authentication

OAuth2 **client_credentials** only. The adapter POSTs
`{api_base_url}/oauth/token` with HTTP Basic auth (`client_id:client_secret`)
and receives a short-lived bearer (~11 minutes) — non-standard response
shape: the token is under `value` (not `access_token`) with its TTL in
`expiresIn`, and **no refresh token** exists. The transport caches the
bearer, re-mints ahead of expiry, and retries once on 401.

`client_secret` is stored Fernet-encrypted in `AdapterConfig.config_json`
(requires the `ENCRYPTION_KEY` environment variable); bearers live in memory
only. Credentials are sent via the `Authorization` header, never query
parameters, and upstream response bodies are redacted before logging.

## Capabilities

- **Pricing**: CPM only (validated against live line items).
- **Targeting**: placement/package inventory selection, geo
  country/region targeting (resolved against the platform geo dictionary —
  ISO alpha-2 codes, CLDR names, and platform display names all accepted;
  buyer excludes win over product includes). Postal areas, metros/DMA,
  AdCP `geo_regions` overlays (ISO 3166-2 vs the platform's continental
  regions), and proximity targeting are rejected loudly up front.
- **Inventory sync**: paginated sweep of placements (+publishers derived
  inline), packages, and sizes into the `improvedigital_inventory` cache —
  committed page-by-page; feeds the product-config pickers and the
  inventory browser. Rate limit: 100 reads/60s upstream — the transport
  sleeps and retries on 429.
- **Reporting sync**: Report API preview (≤500 rows) with dimensions
  `campaign_id, line_item_id`, upserted into `improvedigital_line_item_stats`
  (spend stored as micros). `get_media_buy_delivery` aggregates this cache
  and raises `DeliveryDataUnavailable` while it's empty — no fake zeros.
- **Updates**: pause/resume (buy + package), budget/impression updates
  (read-modify-write of the full line-item DTO), archive; approval actions
  are platform no-ops (Classic campaigns have no approval workflow).
- **No webhooks, no realtime reporting.**

## Configuration

1. In the tenant's **Ad Server** settings, select **Improve Digital** and
   enter the OAuth2 `client_id` / `client_secret` and `api_base_url`
   (production: `https://api.360yield.com`).
2. **Test Connection** verifies the credentials and auto-fills the API
   user's `improve_demand_contact_id` and `business_unit_id`.
3. Discover (admin-scoped credentials) or manually enter
   `buying_entity_id` + `buying_entity_office_id` — the Classic campaign
   API rejects campaigns without them.
4. Optionally fill the **campaign metadata attribution** fields
   (advertiser/brand UUID, agency, DSP seat, ad-ops/sales owners) — when
   set, every booked campaign gets a `CampaignMetadataDto` record.
5. Run **Sync Inventory**, then build products from the synced placement /
   package / size pickers. Products without `placement_ids` or
   `package_ids` fail booking loudly — a Classic line item with no
   inventory selection would serve nowhere.

A scripted alternative: `uv run python scripts/ops/configure_improvedigital_tenant.py`
(credentials via environment variables).

## Reference

- [api-requests.md](api-requests.md) — every request the adapter sends,
  with payload examples.
- [live-wire-shapes.md](live-wire-shapes.md) — live-validated wire-shape
  constraints (datetime formats, assignment envelopes, geo requirements,
  size resolution, multipart creative servlets).
