"""Tenant reporting currency: the ad server's own currency.

Every publisher-facing money figure that is not a buyer's budget — delivery
snapshots (``delivered_amount``), dashboard revenue, the reporting pages —
is denominated in the currency the ad server reports in: the GAM network
currency, or the Improve Digital buying-entity-office currency stored in
``config_json["currency"]``. ``preferred_wholesale_currency`` already knows
both; this module is the single resolver the admin surfaces share, so none
of them falls back to an arbitrary (alphabetical) currency limit while an
ad server currency is configured.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.core.database.repositories.adapter_config import AdapterConfigRepository
from src.core.database.repositories.currency_limit import CurrencyLimitRepository
from src.core.inventory_profile_projection import preferred_wholesale_currency

DEFAULT_REPORTING_CURRENCY = "EUR"


def tenant_reporting_currency(session: Session, tenant_id: str) -> str:
    """ISO 4217 code the tenant's ad server reports spend in.

    Resolution order: ad server currency (GAM network currency / Improve
    Digital office currency) → lowest currency-limit code → ``EUR``.
    """
    adapter_config = AdapterConfigRepository(session, tenant_id).find_by_tenant()
    configured = preferred_wholesale_currency(adapter_config)
    if configured:
        return configured
    codes = sorted(str(limit.currency_code) for limit in CurrencyLimitRepository(session, tenant_id).list_all())
    if codes:
        return codes[0]
    return DEFAULT_REPORTING_CURRENCY
