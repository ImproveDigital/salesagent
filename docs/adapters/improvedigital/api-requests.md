# Improve Digital (360Yield) — every request the adapter sends

Complete inventory of the HTTP calls `src/adapters/improvedigital/` makes,
with the exact bodies we build, for cross-checking against the platform's
API documentation.

Every request below has a runnable counterpart in
`improvedigital-marketplace.postman_collection.json` (same folder), with the
same bodies.

Host comes from the tenant's `api_base_url` (`https://api.360yield.com`
production; Improve Digital issues per-environment hosts). Payload examples
use placeholder identity values: demand contact `7001`, buying entity
`9001`, office `9002`, business unit `44`, buyer `30`.

## Contents

| Flow | Trigger | Section |
|---|---|---|
| Auth | every call | [1](#1-authentication) |
| Permission probes | Test Connection, permissions report | [2](#2-permission-probes-read-only) |
| Adapter settings discovery | Admin UI buttons | [3](#3-admin-ui-discovery) |
| Booking | media buy approve | [4](#4-booking--create_media_buy) |
| Creatives | creative upload/assignment | [5](#5-creatives) |
| Buy updates | pause/resume/budget/archive | [6](#6-update_media_buy) |
| Status | delivery polling | [7](#7-status) |
| Reporting | reporting sync scheduler | [8](#8-reporting-sync) |
| Inventory | "Sync Inventory Now" + scheduler | [9](#9-inventory-sync) |

Every request carries `Authorization: Bearer <token>` and
`accept: application/json`; JSON bodies add `Content-Type: application/json`.
A 401 re-mints the bearer and retries once; a 429 sleeps 61s and retries
(two attempts, at +20s and +65s). Default timeout 30s, 90s for booking.

---

## 1. Authentication

`src/adapters/improvedigital/_transport.py`

### Mint a bearer

```
POST /oauth/token
Authorization: Basic base64(client_id:client_secret)
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
```

Bearer is read from the response's `value` field (not `access_token`), TTL
from `expiresIn`. Re-minted 2 minutes before expiry; there is no refresh
token.

### Release a bearer (best effort, on client close)

```
DELETE /oauth/logout/{token}
```

---

## 2. Permission probes (read-only)

`check_permissions()` — one GET per adapter concern, non-raising, no bodies.

| Method | Path | Concern | Required |
|---|---|---|---|
| GET | `/rtb/v1/classic/campaigns?limit=1` | media buy lifecycle | yes |
| GET | `/rtb/v1/classic/line-items?limit=1` | media buy lifecycle | yes |
| GET | `/rtb/v3/placements?limit=1` | inventory | yes |
| GET | `/schema/rtb/v1/classic/campaigns/campaign` | create_media_buy | yes |
| GET | `/rtb/v1/sizes-all` | creative formats | no |

---

## 3. Admin UI discovery

`src/admin/blueprints/adapters.py`

| Button | Requests |
|---|---|
| **Test Connection** | `GET /rtb/v1/classic/campaigns?limit=1`, then `GET /lookup/v1/user-details` |
| **Discover from API** (entities) | `GET /admin/v1/buying-entities-combo?limit=100&offset=N` (paginated) |
| **Discover from API** (offices) | `GET /admin/v1/buying-entities/{buying_entity_id}/buying-entity-offices?limit=100&offset=N` |
| **Campaign Metadata** (advertiser picker) | `GET /api/metadata-advertisers?search=<term>` |
| **Campaign Metadata** (agency picker) | `GET /api/metadata-agencies?search=<term>` |
| **Expand a package** (product config) | `GET /rtb/v1/packages/{package_id}/placements` |

`user-details` supplies `user_id` (→ `improve_demand_contact_id`),
`business_unit_id`, and the buyer list behind the Buyer ID picker. Office
rows are filtered to `active == true` and `"Classic" in buying_types`.

---

## 4. Booking — `create_media_buy`

Fires on media buy approval, in this order. Any failure triggers cleanup
(§4.6).

### 4.1 Create the campaign

```
POST /rtb/v1/classic/campaigns
```
```json
{
  "name": "adcp_PO-12345",
  "type": "Improve",
  "start_date": "2026-08-10 10:20:28",
  "end_date": "2026-09-10 23:59:59",
  "time_zone": "Europe/Amsterdam",
  "currency": "EUR",
  "improve_demand_contact_id": 7001,
  "buying_entity_id": 9001,
  "advertiserId": null,
  "buying_entity_office_id": 9002,
  "buying_entity_office_ids": [9002],
  "agencyId": 123
}
```

- `name` is `adcp_<po_number>`, falling back to `adcp_<unix_timestamp>`.
- `buying_entity_office_id` / `buying_entity_office_ids` and `agencyId` are
  omitted when not configured.
- `advertiserId` is the principal's platform mapping, but only when it is
  numeric — metadata-advertiser UUIDs are sent as `null`.
- The response `id` becomes the media buy's external ID
  (`improvedigital_<campaign_id>`).

### 4.1b Attach campaign metadata (when configured)

```
POST /api/metadata-campaigns
```
```json
{
  "campaignId": "370306",
  "campaignName": "adcp_PO-12345",
  "campaignStartDate": "2026-08-10T10:20:28.000Z",
  "campaignEndDate": "2026-09-10T23:59:59.000Z",
  "currencyCode": "EUR",
  "entityType": "c",
  "completed": true,
  "isCompleted": true,
  "advertiserUuid": "00000000-0000-4000-8000-000000000001",
  "advertiserName": "Other",
  "agencyId": 182,
  "agencyName": "Other",
  "businessUnitId": 44,
  "buyerId": 30,
  "integrationPlatformId": 1,
  "seatId": "default",
  "adOpsPersonId": 17373,
  "salesPersonId": "f1b6846f-659b-426a-be89-c2c0b99be27c"
}
```

A surface parallel to booking: `CampaignMetadataDto` carries the commercial
attribution the Classic `CampaignDto` has no room for. Posted right after
campaign create so a rejection cleans up the campaign before any line item
exists. Skipped entirely when the tenant configured no metadata fields.

Notes for cross-checking:

- **camelCase**, unlike the snake_case booking API.
- Dates are **ISO-8601 UTC strings with milliseconds**
  (`2026-08-14T14:45:18.407Z`), not the `YYYY-MM-DD HH:MM:SS` local strings
  the booking API takes.
- `campaignId` is a **string** in the body, though the Classic campaign id
  is an integer. It is also the only schema-required field — everything else
  is omitted when unset.
- `entityType: "c"`, `completed` and `isCompleted` are fixed values pending
  confirmation of what the platform derives on its own.
- Attribution is tenant-level today — every buy books under the same brand,
  agency and owners. Per-buyer routing is plan item H2.

Read the record back with
`GET /api/metadata-campaigns/integration-platform/{integrationPlatformId}/campaign/{campaignId}`.

### 4.2 Create one line item per package

```
POST /rtb/v1/classic/campaigns/{campaign_id}/line-items
```
```json
{
  "name": "TEST_Valid_Flight_Budget_Aug26",
  "type": "Standard",
  "line_item_status": "Active",
  "goal": "BUDGET",
  "start_date": "2026-08-10 10:20:28",
  "end_date": "2026-08-10 23:59:59",
  "time_zone": "UTC",
  "cpm_bid": 1.0,
  "pricing_model": "CPM",
  "pricing_model_type": "First Bid",
  "impression_cap": 2000,
  "impression_cap_daily": false,
  "budget_is_daily": false,
  "invoice_type": "on_actuals",
  "delivery_schedule": "Evenly",
  "third_party_inventory": true,
  "is_optimised": true,
  "is_dynamic_optimization": false,
  "dynamic_optimization_kpi_type": "",
  "dynamic_optimization_kpi_value": 0,
  "conversion_tracking_enabled": false,
  "keep_on_delivering": false,
  "track_viewability": false,
  "is_consentless": false,
  "is_coppa_compliant": false,
  "optout_mechanism": [],
  "reference_number": "pkg_1",
  "improve_demand_contact_id": 7001,
  "budget": 2.0,
  "flight_details": [
    {
      "start_time": "2026-08-10 10:20:28",
      "end_time": "2026-08-10 23:59:59",
      "budget": 2.0,
      "budget_is_daily": false,
      "impression_cap": 2000,
      "impression_cap_daily": false
    }
  ],
  "business_unit_id": 44,
  "buyer_id": 30,
  "frequency_cap": 3,
  "frequency_interval": 1,
  "frequency_interval_type": "days",
  "placement_ids": [98765],
  "package_ids": [4321],
  "size_ids": [4]
}
```

Field sources:

| Field | Source |
|---|---|
| `name` | package name, falling back to package ID |
| `goal` | product config `goal` (default `BUDGET`) |
| `cpm_bid` | package pricing — `rate` when fixed, else `bid_price` |
| `budget` | package budget; when the buy carries budget only at buy level, derived as `impression_cap × cpm_bid ÷ 1000` |
| `impression_cap` | budget ÷ rate × 1000, computed by the core layer before dispatch |
| `pricing_model` | product config, else `CPM` (`FLAT_RATE` for flat-rate pricing) |
| `delivery_schedule`, `frequency_*` | product config; `delivery_schedule` defaults to `Evenly`, the frequency keys are omitted when unset |
| `reference_number` | our internal package ID, for reconciliation |
| `improve_demand_contact_id`, `business_unit_id`, `buyer_id`, `time_zone` | tenant adapter config (`business_unit_id`/`buyer_id` omitted when unset) |
| `placement_ids`, `package_ids`, `size_ids` | product config |

Notes for cross-checking:

- **No `currency`** — the platform requires it to match the campaign owner's
  default and 400s when sent.
- **No `id` / `campaign_id`** in the body: `id` is server-assigned and the
  campaign is already in the path. The platform's own UI sends `id: 0` and a
  body `campaign_id`; add them if the API validates them.
- `flight_details` is **not** in the committed `rtb-v3-openapi.json` — the
  closest documented fields are `custom_budgets` and `daily_impression_caps`.
- `dynamic_optimization_kpi_value` is typed `string` in that spec but sent as
  the number `0`, matching the platform's own payload.
- `placement_ids` / `package_ids` / `size_ids` are not part of
  `CommonDealLineItemDto`; the server ignores them and the real assignment
  happens in §4.3–4.4.
- `frequency_interval_type` must be one of the spec's lowercase values:
  `months`, `weeks`, `days`, `hours`, `minutes`.

### 4.3 Assign placements (when the product pins any)

```
PUT /rtb/v1/classic/campaigns/{campaign_id}/line-items/{line_item_id}/placements
```
```json
{"line_item_placements": [{"id": 98765, "assigned": true}, {"id": 98766, "assigned": true}]}
```

### 4.4 Assign packages (when the product pins any)

```
PUT /rtb/v1/classic/campaigns/{campaign_id}/line-items/{line_item_id}/packages
```
```json
{"line_item_packages": [{"id": 4321, "assigned": true}]}
```

A package with neither placements nor packages aborts the booking — the line
item would target no inventory.

### 4.5 Geo targeting (only when the buy or product sets geo)

Resolving platform geo names first, once per adapter instance:

```
GET /rtb/v1/regions?limit=100&offset=N
GET /rtb/v1/regions/{regionName}/countries?limit=100&offset=N
```

Then, per line item:

```
PUT /rtb/v1/classic/campaigns/{campaign_id}/line-items/{line_item_id}/geo-targeting
```
```json
{
  "filter": true,
  "geo_targeting": [
    {"country": "Netherlands", "exclude": false, "region": "EMEA"},
    {"country": "Belgium", "exclude": true, "region": "EMEA"}
  ]
}
```

`exclude` **and** `region` are required on every entry, includes included.
Country tokens (ISO alpha-2 from the buyer, or platform names from the
product) are rewritten to the platform's exact spelling; an unresolvable
token fails the booking rather than being dropped.

### 4.6 Cleanup after a partial failure

```
DELETE /rtb/v1/classic/campaigns/{campaign_id}
PUT    /rtb/v3/campaigns/{campaign_id}/archive     # fallback when delete is refused
```

Delete is refused once the campaign has served impressions.

---

## 5. Creatives

### 5.1 Resolve the platform size (cached per adapter instance)

```
GET /rtb/v1/sizes-all
```

Matched on width × height; the resolved `name` + `id` are added to the
creative body. A bare `"300x250"` string is rejected by the platform.

### 5.2a Tag creatives — plain JSON bulk upload

```
POST /rtb/v1/classic/campaign/creatives/third-party-tag/bulk-upload
```
```json
{
  "campaign_id": 55501,
  "creative_type": "Third Party Tag",
  "creatives": [
    {
      "name": "banner_300x250",
      "size": "300x250",
      "size_id": 4,
      "width": 300,
      "height": 250,
      "status": "Active",
      "tag": "<script src=\"https://cdn.example.com/ad.js\"></script>",
      "advertiser_domain": "example.com",
      "third_party_type": "display",
      "platform_types": ["Web"],
      "tag_secure": true
    }
  ]
}
```

Hosted-image assets are wrapped into an `<img>` (optionally inside an `<a>`)
before being sent as a tag — the endpoint validates that the tag is real
HTML.

### 5.2b Other creative types — multipart servlet

```
POST /rtb/v1/classic/campaigns/{campaign_id}/creatives
Content-Type: multipart/form-data
```

The `CreativeDto` travels as a `body` part with content type
`application/json`; same fields minus `third_party_type` / `platform_types` /
`tag_secure`. Type is derived from the asset: `VAST Audio`, `VAST`, `Native`,
otherwise `Third Party Tag`. Plain JSON to this path returns HTTP 500.

### 5.3 Recover a creative ID (only when the create response carries none)

```
GET /rtb/v1/classic/campaigns/{campaign_id}/creatives
```

Matched by name, highest ID wins.

### 5.4 Bind creatives to a line item

```
PUT /rtb/v1/classic/campaigns/{campaign_id}/line-items/{line_item_id}/creatives
```
```json
{"line_item_creatives": [{"id": 778899, "assigned": true}]}
```

One call per line item, assigning every creative. Can take >30s on dev.

---

## 6. `update_media_buy`

| Action | Requests |
|---|---|
| `pause_media_buy` / `resume_media_buy` / `activate_order` | `GET /rtb/v1/classic/campaigns/{cid}/line-items`, then `PUT /rtb/v3/campaigns/{cid}/line-items/{lid}/status?active=true\|false` per line item |
| `pause_package` / `resume_package` | `PUT /rtb/v3/campaigns/{cid}/line-items/{lid}/status?active=true\|false` |
| `update_package_budget` | `GET /rtb/v1/classic/campaigns/{cid}/line-items/{lid}`, mutate `budget`, `PUT` the full DTO back to the same path |
| `update_package_impressions` | same read-modify-write, mutating `impression_cap` |
| `archive_order` | `PUT /rtb/v3/campaigns/{cid}/archive` |
| `submit_for_approval` / `approve_order` | **no request** — Classic campaigns have no approval workflow |

Status toggles are query-param PUTs with no body. Budget updates are
read-modify-write because the platform's PUT expects the complete DTO.

---

## 7. Status

```
GET /rtb/v1/classic/campaigns/{campaign_id}
```

`get_media_buy_delivery` makes **no** API call — it aggregates the local
`improvedigital_line_item_stats` cache filled by the reporting sync, and
raises `DeliveryDataUnavailable` while that cache is empty.

---

## 8. Reporting sync

```
POST /report/ext/preview
```
```json
{
  "rows": 500,
  "report_generation_request": {
    "title": "",
    "report_type": "EXT_CONSOLIDATE",
    "currency_id": 1,
    "date_range": {"quick": "LAST_31_DAYS"},
    "dimensions": ["campaign_id", "line_item_id"],
    "metrics": ["impressions", "clicks", "advertiser_payout", "complete"],
    "filters": [{"column": "campaign_id", "operation": "IN", "value": [555117]}],
    "timezone": "UTC",
    "action": "PREVIEW_REPORT"
  }
}
```

- Snake_case only — the camelCase shape in the OpenAPI spec deserializes to
  a null request (HTTP 500).
- `currency_id`: 1 = EUR, 2 = USD.
- `filters.value` holds the campaign IDs of the tenant's active buys.
- Capped at 500 rows; a full page logs a truncation warning. The async
  `/report/ext/generation` path is not wired up yet.

---

## 9. Inventory sync

Paginated sweeps, 1000 rows per page, up to 500 pages per family:

```
GET /rtb/v3/placements?offset=N&limit=1000
GET /rtb/v1/packages?offset=N&limit=1000
GET /rtb/v1/sizes-all?offset=N&limit=1000
```

Publishers are derived from the inline `publisher_id` / `publisher_name` on
placement rows — there is no buy-side publishers endpoint. Package
membership is deliberately **not** swept (2k+ packages × one request each
would exhaust the 100-reads/60s quota); it is fetched on demand when an
operator expands a package (§3).

---

## Client methods with no caller

Defined in `client.py` but not reached by any adapter or admin flow today —
useful when scoping which endpoints actually need API permissions:

`list_campaigns`, `delete_line_item`, `list_all_line_items`,
`archive_line_item`, `assign_placements` / `unassign_placements` (the v2
`/placements/assign` surface — booking uses the v1 `/placements` PUT
instead), `list_placements`, `get_creative`, `update_creative`,
`delete_creative`, `set_creative_status`, `list_line_item_creatives`,
`unassign_all_creatives`, `validate_vast_url`, `creative_type_sizes`,
`countries` (`/common/v1/countries`), `submit_generation`,
`generation_status`, `allowed_filters`.
