# POC: GAM delivery reporting in the buy currency

Branch: `poc/gam-rest-reporting-currency` (from `develop`, 2026-10-13).

## Problem

The GAM SOAP `ReportService` reports `AD_SERVER_CPM_AND_CPC_REVENUE` in the
network currency only. A buy booked in AUD on the Azerion Gaming network (EUR)
delivered EUR figures; the media buy page labelled them AUD and the AdCP
`get_media_buy_delivery` response labelled them AUD per package. The
network-currency label fix (`b3e1731d7`) made the admin page honest but still
EUR. Buyers want spend in the currency they booked in.

## What the POC does

- `src/adapters/gam/reporting_rest.py`: minimal client for the REST Reporting
  API (`admanager.googleapis.com/v1`). Create report, run, poll the operation,
  page `fetchRows`, decode `ReportValue`s. `ReportDefinition.currencyCode`
  carries the requested currency.
- `GAMAuthManager.get_rest_auth_header()`: bearer token for the REST API.
  Service accounts mint a token for `auth/admanager.readonly`; OAuth refresh
  tokens reuse the googleads client (see open questions).
- `GAMReportingService.get_reporting_data(..., currency_code=)`: routes through
  REST when a currency is given, normalises rows to the SOAP CSV column shape
  so `_process_report_data` is shared. `ReportingData.currency_code` records
  the currency used.
- `GoogleAdManager.get_media_buy_delivery`: requests the buy currency when it
  differs from the network currency and is in `gam_secondary_currencies`
  (captured by the connection test). Otherwise, or if the REST call fails,
  falls back to the SOAP network-currency report and labels it honestly.
- `get_media_buy_delivery` tool: per-package `currency` follows the adapter's
  reported currency. Response-level currency keeps the existing buy-currency
  contract (`tests/unit/test_delivery_response_currency.py`).

Consumers: admin media buy page and the AdCP tool share the adapter path, so
both get AUD for an AUD buy once the network can report AUD.

## Verify on dev (not yet done)

1. Confirm the Azerion Gaming network lists AUD under secondary currencies
   (Settings → GAM → test connection stores `gam_secondary_currencies`).
2. Open the AUD media buy page; expect `AUD <converted amount>` and a log
   line `GAM REST report ... (currency=AUD)`.
3. Call `get_media_buy_delivery` for the buy; expect `by_package[].currency ==
   "AUD"` and the same spend as the page.
4. If the page shows EUR, look for `REST delivery report in AUD failed` in the
   logs; the message carries the HTTP status and body.

## Verified against the Azerion Gaming network (2026-10-13, local server)

Order 4216426454 (media buy `mb_34875186c978`, booked in AUD), REST report
with and without `currencyCode`:

| currency | impressions | `AD_SERVER_REVENUE_WITHOUT_CPD` raw |
|---|---|---|
| AUD | 1002 | `{"doubleValue": 1.0019291171757052}` |
| network (EUR) | 1002 | `{"doubleValue": 0.623178}` |

- **Money encoding: `doubleValue` in currency units**, not micros.
  `money_to_micros` scales floats by 1e6, so the SOAP-shaped row carries
  micros as before. The EUR figure matches the SOAP report (0.62).
- **Scope: report creation needs the full `auth/admanager` scope.** The
  read-only scope gets `ACCESS_TOKEN_SCOPE_INSUFFICIENT` on
  `ReportService.CreateReport` (the discovery doc lists only the full scope
  for `create`). Service accounts mint it on demand. The first local run used
  the read-only scope, so the adapter fell back to SOAP and labelled EUR,
  which looked like "still network currency". Fixed in `GAMAuthManager`.
- **Dimension values**: IDs arrive as `intValue`, names as `stringValue`.
  `DATE` was not exercised (delivery uses `all_time` without it).

## Still open

- **OAuth tenants.** Their refresh tokens were granted `auth/dfp`; the REST
  API will refuse them the same way. They need re-consent with the REST scope
  or a service account. Only `tenant_azerion_gaming` and `default` use
  service accounts locally.
- **Report accumulation.** Each run creates a hidden report resource (two
  were created by the verification above); there is no delete endpoint.
  Production should find-or-create one report per (network, currency,
  dimensions) and `patch` its date range and filters.
- **`delivered_amount`.** The persisted snapshot holds the buy-currency
  figure when conversion happens. Dashboards pace it against the buy budget,
  which is the intended comparison, but rows synced earlier were EUR.

## Not in this POC

- Currency dropdown on the GAM Reporting page (same REST runner, separate UI
  work).
- Response-level `currency` keyed on the adapter's label instead of the buy
  currency.
