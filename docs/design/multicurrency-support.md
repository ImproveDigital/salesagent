# Multicurrency support: GAM secondary currencies and Improve Digital offices

Status: proposal (2026-10-02). Nothing in this document is implemented yet.

## 1. Problem

Three independent things are broken or missing today:

1. **GAM delivery spend is mislabelled for secondary-currency buys.** A buy priced in a GAM secondary currency
   (e.g. EUR on a USD network) is booked correctly, but the GAM report returns revenue in the *network* currency and
   the delivery response stamps it with the *buy* currency. No conversion happens anywhere.
2. **Improve Digital silently books the wrong price when the buyer picks a non-office currency.** One tenant has one
   buying entity, one office and one currency. The campaign is always created in the configured currency; the buyer's
   pricing-option currency is ignored and the numeric rate is passed through unconverted.
3. **Delivery reporting assumes one currency per response.** `get_media_buy_delivery` emits a single top-level
   `currency`, sums spend only for the first buy's currency and drops the rest into a `mixed_currency` error.

The root cause of all three is the same: the system has no shared notion of "which currencies this tenant can
*transact* in", so each layer (admin, discovery, booking, adapter, reporting) improvises its own default (mostly `USD`).

## 2. What AdCP says

Source: adcp 7.0.2 bundles spec 3.1.15 (`.venv/.../adcp/_schemas/3.1`); the repo's live validator cache
(`schemas/v1`, "latest", downloaded 2026-09-23) is already 3.2.x. The rules below hold in both unless noted.

| Rule | Where |
|---|---|
| Currency belongs to the **pricing option**. One product may offer options in several currencies. | `pricing-option.json` (`currency` required on every variant) |
| Buyers filter discovery with `filters.pricing_currencies`; sellers **MUST** return only matching options. | `product-filters.json` |
| A media buy has **exactly one currency**; every selected pricing option must declare it; an update may not redenominate. | `create-media-buy-request.json`, `update-media-buy-request.json` (3.2: `total_budget.currency MUST equal the existing media-buy currency`) |
| **AdCP does not perform currency conversion**; `Sellers MUST NOT perform currency conversion` (bidding policy). | `get-media-buy-delivery-response.json`, `core_bidding-policy.json` |
| Delivery 3.1.15: top-level `currency` required; `by_package[].currency` required. | library `GetMediaBuyDeliveryResponse` |
| Delivery 3.2: top-level `currency` and `aggregated_totals` **deprecated** (removed in 4.0). New `media_buy_deliveries[].currency` per row; for AdCP-authored buys it **MUST equal the buy currency** and every `by_package[].currency` MUST equal it. One response may contain rows in different currencies. | `schemas/v1/...get-media-buy-delivery-response_json.json` |
| No account-level or tenant-level currency exists in the protocol. | `account.json`, `get-adcp-capabilities-response.json` |

Design consequence: **currency is decided once, at the pricing option, and frozen on the media buy. The seller never
converts. If an ad server cannot book *and* report in a currency, that currency must not be offered.**

## 3. Current state (verified in code)

### 3.1 Core

- Supported currencies = `CurrencyLimit` rows per tenant (`src/core/database/models.py:658`). No tenant default
  currency column; "default" is the first row (`tenant_management_api.py:533`) or USD.
- `PricingOption.currency` is the only currency column on products (`models.py:639`). The admin product form offers
  the `CurrencyLimit` codes but **does not check** that an option's currency is bookable on the ad server.
- `create_media_buy` derives one currency from the selected pricing options and rejects mixed packages
  (`media_buy_create.py:2273` `_derive_single_request_currency`), requires a `CurrencyLimit` row (`:2935`), and, for
  GAM only, requires primary or secondary network currency (`:2949`). Stored as `MediaBuy.currency` (`models.py:1125`).
- `update_media_buy` accepts a `Budget` object whose currency differs from `MediaBuy.currency` without rejecting it
  (`media_buy_update.py:963`, `:1279`, `:1841`). `MAX_CAMPAIGN_BUDGET_USD` is applied to every currency (`:1870`).
- `filters.pricing_currencies` is **not implemented** (no hits in `src/` or `core/`).
- No FX code exists anywhere in `src/`.

### 3.2 GAM

- `AdapterConfig.gam_network_currency` / `gam_secondary_currencies` are read from `getCurrentNetwork()` on connection
  test, but only written when empty (`src/admin/blueprints/gam.py:1148`). A `CurrencyLimit` is auto-created for the
  primary currency only (`gam.py:470`).
- Booking is already multicurrency-correct: order `totalBudget` and line item `costPerUnit` use the buy currency
  (`gam/managers/orders.py:156`, `:936`). GAM fixes a line item's currency at creation and converts to network currency
  at serving time using its own rates (GAM help 177495).
- Reporting uses SOAP `ReportService` with `AD_SERVER_CPM_AND_CPC_REVENUE` (`gam_reporting_service.py:388`). That
  column is in **network currency** ("Revenue reporting is only displayed in your network currency"). The delivery
  response labels it `media_buy.currency` (`google_ad_manager.py:1374`). This is the mislabelling bug.
- The REST Reporting API (`networks.reports`, beta) has `ReportDefinition.currencyCode`: "The ISO 4217 currency code
  for this report. Defaults to publisher currency code if not specified." GAM applies the previous day's rate per day.
- Price guidance, forecasts and the admin dashboards all use the network currency (`gam_pricing_availability_sync.py:162`,
  `dashboard_service.py:363`, `operations.py:235`).

### 3.3 Improve Digital

- Exactly one `buying_entity_id`, one `buying_entity_office_id`, one `currency` per tenant
  (`src/adapters/improvedigital/schemas.py:54-176`). Offices are per currency; the office picker only *warns* when the
  office's `billing_currency_code` differs from the configured currency (`connection_config.html:633`).
- Campaign payload uses `self.currency` from config (`adapter.py:816`); line items may not carry a currency at all
  (platform 400s: "Currency field does not match default campaign owner/publisher currency", `adapter.py:936`).
- `_resolve_pricing_rate` (`src/adapters/base.py:602`) ignores `package_pricing_info["currency"]`, so a USD-priced
  option becomes a EUR campaign at the same number.
- Reporting requests one `currency_id` per report (`reporting_sync.py:181`) and stamps rows with the configured
  currency (`:550`). `ImproveDigitalLineItemStats.currency` already exists per row (`models.py:2738`).

### 3.4 Delivery

- `_aggregate_spend_by_currency` (`media_buy_delivery.py:856`): first buy's currency wins, other currencies excluded
  with a `mixed_currency` error. `MediaBuyDeliveryData` has no per-row currency.
- Hard-coded `"USD"` on early returns (`:176`, `:197`, `:227`), in `send_delivery_webhook` default
  (`webhook_delivery_service.py:212`) and in `dynamic_pricing_service.py:187`.

## 4. Proposal

### 4.1 One source of truth: the tenant's transactable currency set

Define `transactable_currencies(tenant) = CurrencyLimit codes ∩ adapter.supported_currencies()`.

Add an adapter capability on `AdServerAdapter` (`src/adapters/base.py`):

```python
def supported_currencies(self) -> frozenset[str] | None:
    """Currencies this ad server can both book and report in. None = unrestricted (mock)."""
```

| Adapter | Returns |
|---|---|
| GAM | `{gam_network_currency} ∪ gam_secondary_currencies` |
| Improve Digital (phase 1) | `{office billing currency}` |
| Improve Digital (phase 2) | one currency per configured office |
| mock / others | `None` |

Enforce the set at every layer, replacing the ad-hoc defaults:

1. **Admin product form and `tenant_management_api` product endpoints**: reject a pricing option whose currency is not
   transactable. Today there is no server-side check.
2. **`get_products`**: implement `filters.pricing_currencies` (prune pricing options, drop products with none left,
   report the exclusion count). Always prune options that are not transactable, so a buyer can never select one.
3. **`create_media_buy`**: replace the GAM-specific block at `media_buy_create.py:2949` with the capability check so
   Improve Digital rejects foreign currencies instead of booking them. Error: `INVALID_PRICING_OPTION` with the
   `currency` detail (spec has no dedicated currency error code).
4. **`update_media_buy`**: reject a `Budget` whose currency differs from `MediaBuy.currency` (spec: no redenomination).
5. **Budget caps**: `MAX_CAMPAIGN_BUDGET_USD` becomes per-currency, read from `CurrencyLimit` (the table already has
   `max_daily_package_spend`; add `max_campaign_budget`).

No FX conversion is introduced. A buyer who wants another currency uses another pricing option.

### 4.2 GAM: report in the buy currency

Booking needs no change. Two reporting changes:

1. **Fixed-price buys (STANDARD / SPONSORSHIP line items, `is_fixed=True`)**: spend in the buy currency is
   `delivered_units × booked rate`, which is what GAM invoices for a fixed-rate line item in its own currency. Compute
   it from the report's impression/click counts and the stored `pricing_info.rate`. No FX needed and exact. This
   covers every guaranteed buy and can ship first.
2. **Non-fixed buys (PRICE_PRIORITY etc.)**: GAM's revenue figure is the only truth and is network-denominated. Run the
   delivery report through the REST Reporting API with `currencyCode = buy currency`, grouping media buys by currency
   (one report per currency per window). Until that migration lands, report these buys with `spend` omitted and a
   `data_unavailable`-style error naming the reason, rather than a wrong number under the wrong label.

Hygiene in the same change:

- Connection test must **refresh** `gam_network_currency` / `gam_secondary_currencies` every run (drop the
  "only if empty" guard at `gam.py:1148`) and create `CurrencyLimit` rows for secondaries as well.
- Add `LINE_ITEM_CURRENCY_CODE` as a dimension attribute to the report so imported/legacy orders carry their currency
  into `GAMOrder.currency_code`.
- Admin dashboards: group spend by currency; never sum across.

Note for the business: GAM's secondary currencies require Ad Manager 360. If Azerion's network is not 360, GAM is
single-currency and `supported_currencies()` returns only the network currency. The rest of this design still applies.

### 4.3 Improve Digital: one office per currency

The platform constraint is hard: a campaign's currency is the office's billing currency and line items inherit it.
Multicurrency therefore means **multiple offices**, not multiple currencies on one office.

**Phase 1 (unblock, small):**

- Validate on save that `config.currency == office.billing_currency_code` (turn the UI warning into a server error in
  `save_adapter_config`, `src/admin/blueprints/adapters.py:262`).
- `supported_currencies()` returns `{config.currency}`. With 4.1 in place, products on an Improve tenant can only carry
  options in that currency and foreign-currency buys are rejected before booking.

**Phase 2 (true multicurrency):**

- Config becomes a list: `buying_entity_offices: [{office_id, currency, improve_demand_contact_id?, buyer_id?}]`, with
  the current scalar fields migrated as a one-element list. The admin picker already receives `billing_currency_code`
  per office from `discover-buying-entities`.
- `_campaign_payload` selects the office whose currency equals the buy currency and sets `currency` from it; `_line_item_payload`
  stays currency-free.
- `reporting_sync` issues one report request per configured currency (`currency_id` from `resolve_currency_id`),
  filtered to that currency's campaigns, and stamps each `ImproveDigitalLineItemStats` row with the campaign currency
  (column already exists). `media_buy.delivered_amount` stays in the buy currency.
- Delivery response uses the campaign's `currency` field (already preferred at `adapter.py:1477`).

**Fallback if Azerion only ever gets one office per buying entity:** keep phase 1 and run one Improve Digital tenant
per currency. Everything above still holds; only the operator setup differs.

### 4.4 Delivery response: per-row currency, no cross-currency totals

- Add `currency` to `MediaBuyDeliveryData` (our subclass in `src/core/schemas/delivery.py`) and populate it from
  `MediaBuy.currency`. `by_package[].currency` must equal it for AdCP-authored buys (3.2 verifier rule
  `row_currency_consistency`). The live validator already runs 3.2 schemas, so the field is accepted now; the library
  type gains it when adcp is upgraded past 3.1.15.
- While the library still requires top-level `currency`: keep today's "first currency wins + `mixed_currency` error"
  for mixed responses, but populate `aggregated_totals` **only** when all rows share a currency; otherwise omit it
  (3.2: "Sellers SHOULD omit this field"). After the library upgrade drop top-level `currency` and `aggregated_totals`
  entirely.
- Replace every hard-coded `"USD"` on the delivery path with the buy currency: early returns in
  `media_buy_delivery.py`, `send_delivery_webhook` callers, `dynamic_pricing_service.py`, GAM dry-run paths.
- Webhooks are already per buy (`delivery_webhook_scheduler.py:261`), so each carries one currency once the default
  is fixed.

## 5. Delivery order

| Step | Scope | Why first |
|---|---|---|
| 1 | 4.1 capability + `create_media_buy` / `update_media_buy` enforcement; Improve Digital phase 1 | Stops wrong-currency bookings today. Small. |
| 2 | 4.4 delivery currency hygiene + per-row currency | Makes every report truthful; prerequisite for mixed-currency responses. |
| 3 | 4.2 GAM fixed-price spend in buy currency; connection-test refresh | Fixes the mislabelling for all guaranteed buys without new APIs. |
| 4 | `filters.pricing_currencies` + product authoring validation | Spec compliance; buyers can discover deterministically. |
| 5 | 4.2 REST Reporting API with `currencyCode` | Non-fixed secondary-currency GAM buys. |
| 6 | 4.3 Improve Digital phase 2 (multiple offices) | Needs confirmation from Improve Digital (see §6). |

## 6. Open questions

1. **Improve Digital**: can buying entity 421 own several offices with different `billing_currency_code`s, and does
   Classic campaign create accept any office of the entity? Are `business_unit_id` / `buyer_id` per office? (Arnaud
   Delvoye is the contact from the reference-number work.)
2. **GAM**: is Azerion's network Ad Manager 360 with secondary currencies enabled? Check `gam_secondary_currencies`
   on the production tenant after a connection-test refresh.
3. **GAM REST Reporting API**: is the beta available on the account, and does the service account's OAuth scope
   cover it?
4. **Product**: which currencies are actually needed (EUR, USD, GBP?). This bounds the `CurrencyLimit` seeding.
5. **Existing data**: any media buys already booked on Improve Digital with a non-EUR pricing option must be audited;
   they were created at the wrong price.

## 7. Explicitly out of scope

- FX conversion inside salesagent. The spec forbids seller-side conversion, no rate authority exists, and billing
  reconciliation would break. Where the ad server converts (GAM report currency), we relay its figure and name it.
- A tenant-wide "default currency" column. The spec has no such concept; the transactable set plus the pricing option
  is sufficient.
