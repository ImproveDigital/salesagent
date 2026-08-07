# Improve Digital (360Yield) — live wire shapes (dev platform)

Validated live against `https://api.360yielddev.com` (2026-08-04) via
`tests/integration/test_improvedigital_live.py` (full booking cycle green).
These findings correct/extend the committed OpenAPI spec, which is wrong or
incomplete in several places.

## Booking (Classic)

- **Dates**: all Classic entities take `YYYY-MM-DD HH:MM:SS` strings, never
  bare dates.
- **Campaign create** (`POST /rtb/v1/classic/campaigns`) requires beyond the
  spec: `type: "Improve"`, `buying_entity_id` (421 = "Improve Digital
  Marketplace" on dev), and at least one buying entity office
  (`buying_entity_office_id`/`buying_entity_office_ids`, 5068 on dev) —
  otherwise 500 "Assign at least one Always on Deal buying entity office".
- **Line item create** requires `type: "Standard"`, `line_item_status:
  "Active"`, `business_unit_id` (33 = Azerion on dev),
  `improve_demand_contact_id`, and `goal` for CPM items
  (`IMPRESSION`/`BUDGET`). `pricing_model: "CPM"` confirmed (gap G2 closed).
- **improve_demand_contact_id == the API user's user_id** (from
  `GET /lookup/v1/user-details`) — every recent dev campaign follows this.
- **Advertiser is optional** on Classic campaigns (`advertiserId` is null on
  every live dev campaign); metadata advertisers (`/api/metadata-advertisers`)
  are UUIDs and do not fit `CampaignDto.advertiserId` (integer).
- **Geo targeting** (`PUT .../line-items/{id}/geo-targeting`) requires
  `exclude` AND `region` on every `geo_targeting` entry — includes too —
  despite the spec marking all `Geo` fields optional (400 "object has
  missing required properties [\"exclude\",\"region\"]"). Country entries
  must carry their region, resolved from `/rtb/v1/regions` +
  `/rtb/v1/regions/{name}/countries`.

## Inventory

- **v3 placement search rows** use `placement_id`/`placement_name`, not
  `id`/`name`. Envelope: `{"placements": [...], "total_number_of_elements"}`.
- The placement search serves 1000-row (and larger) pages — keep pages big:
  the API rate-limits reads to **100 requests per 60s** (429 after that,
  and connections stall near the limit). The transport sleeps out the
  window and retries.

## Creatives (Classic)

- Single create (`POST .../creatives`) is a **multipart servlet** (DTO in a
  `body` part) — plain JSON gets 500 "Failed to parse multipart".
- Tag creatives use the plain-JSON
  `POST /rtb/v1/classic/campaign/creatives/third-party-tag/bulk-upload`
  (`CreativeBulkUploadDto`: campaign_id, creative_type, creatives[],
  optional line_item_id).
- `creative_type` canonical names (`GET /common/v1/i18n/creative_type`):
  **Third Party Tag**, Image (PNG, GIF, JPG), Flash (SWF), Html5, VAST,
  Mobile In-App-Third Party Tag, VAST_VPAID, Video File, Native, Ad
  Builder, Raw Video, VAST Audio.
- Tag creatives additionally require `advertiser_domain`,
  `third_party_type: "display"`, `platform_types: ["Web"]` (capital W), and
  a **resolved size**: a bare `"300x250"` string is rejected ("There are no
  available placements with this creative size"); send the display name +
  `size_id` from `GET /rtb/v1/sizes-all` (300x250 → id 4).
- Creative binding (`PUT .../line-items/{id}/creatives`, envelope
  `{"line_item_creatives": [{"id": X, "assigned": true}]}`) can take >30s
  on dev — the adapter uses a 90s timeout.

## Report API

- Deserializes **snake_case**, not the camelCase in the OpenAPI spec —
  camelCase yields 500 "request is null":

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
      "filters": [{"column": "campaign_id", "operation": "IN", "value": [123]}],
      "timezone": "UTC",
      "action": "PREVIEW_REPORT"
    }
  }
  ```

- `currency_id`: 1=EUR, 2=USD.

## Cleanup

- Campaigns with served impressions cannot be hard-deleted ("Campaign can
  not be deleted because it has served impressions") — the dev platform
  attributes simulated impressions within seconds; archive
  (`PUT /rtb/v3/campaigns/{id}/archive`) is the fallback.

## Tenant configuration

```bash
export IMPROVEDIGITAL_CLIENT_ID=...
export IMPROVEDIGITAL_CLIENT_SECRET=...
python scripts/ops/configure_improvedigital_tenant.py \
    --tenant default \
    --demand-contact-id <user_id from /lookup/v1/user-details> \
    --api-base-url https://api.360yielddev.com \
    --buying-entity-id 421 --buying-entity-office-id 5068 \
    --business-unit-id 33 \
    --currency EUR --timezone Europe/Amsterdam
```

Note: the dev API host resolves to a VPN-private address — Docker bridge
containers typically cannot reach it even when the host can. Run the stack
with VPN routing that covers the Docker subnet, or verify from the host via
the live smoke test.
