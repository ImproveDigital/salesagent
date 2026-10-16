"""Tenant reporting currency: the currency publisher-facing money is shown in.

Every publisher-facing money figure that is not a buyer's budget — delivery
snapshots (``delivered_amount``), dashboard revenue, the reporting pages —
is denominated in one currency per deployment:

1. ``REPORTING_CURRENCY`` (environment), when set and the tenant's ad server
   can report in it. GAM (``ReportQuery.reportCurrency``) and Improve
   Digital (Report API ``currency_id``) convert server-side with their own
   exchange rates; the sales agent never converts. Adapters that cannot
   report in another currency (mock, etc.) ignore the setting so a label
   never disagrees with its figures.
2. Otherwise the ad server's own currency: the GAM network currency, or the
   Improve Digital buying-entity-office currency in ``config_json["currency"]``
   (``preferred_wholesale_currency`` knows both).
3. Otherwise the lowest currency-limit code, then ``EUR``.

Buyer-facing AdCP responses are not affected: they are always in the
currency the buy was booked in.
"""

from __future__ import annotations

import os

from sqlalchemy.orm import Session

from src.core.database.repositories.adapter_config import AdapterConfigRepository
from src.core.database.repositories.currency_limit import CurrencyLimitRepository
from src.core.inventory_profile_projection import preferred_wholesale_currency

DEFAULT_REPORTING_CURRENCY = "EUR"
REPORTING_CURRENCY_ENV = "REPORTING_CURRENCY"
# Ad servers whose reporting API accepts a report currency and converts.
CONVERTIBLE_ADAPTER_TYPES = frozenset({"google_ad_manager", "improvedigital"})


def publisher_reporting_currency(adapter_type: str | None) -> str | None:
    """The deployment-wide reporting currency for this ad server, or None.

    None means "report in the ad server's own currency": the setting is
    unset, or the adapter cannot ask its platform for another currency.
    """
    configured = os.environ.get(REPORTING_CURRENCY_ENV, "EUR").strip().upper()
    if not configured or adapter_type not in CONVERTIBLE_ADAPTER_TYPES:
        return None
    return configured


def tenant_reporting_currency(session: Session, tenant_id: str) -> str:
    """ISO 4217 code the tenant's publisher-facing money is reported in.

    Resolution order: ``REPORTING_CURRENCY`` (when the ad server can honour
    it) → ad server currency (GAM network / Improve Digital office) → lowest
    currency-limit code → ``EUR``.
    """
    adapter_config = AdapterConfigRepository(session, tenant_id).find_by_tenant()
    adapter_type = getattr(adapter_config, "adapter_type", None) if adapter_config is not None else None
    override = publisher_reporting_currency(adapter_type if isinstance(adapter_type, str) else None)
    if override:
        return override
    configured = preferred_wholesale_currency(adapter_config)
    if configured:
        return configured
    codes = sorted(str(limit.currency_code) for limit in CurrencyLimitRepository(session, tenant_id).list_all())
    if codes:
        return codes[0]
    return DEFAULT_REPORTING_CURRENCY
