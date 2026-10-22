"""The publisher-facing site currency is the ad server's reporting currency.

GAM tenants: the network currency. Improve Digital tenants: the buying-entity
office currency in ``config_json["currency"]``. Only without either does the
lowest currency-limit code (then EUR) apply — never an alphabetical limit
while an ad server currency is configured.
"""

from unittest.mock import MagicMock, patch

from src.core.tenant_currency import tenant_reporting_currency

_MOD = "src.core.tenant_currency"


def _limits(*codes: str) -> list[MagicMock]:
    rows = []
    for code in codes:
        row = MagicMock()
        row.currency_code = code
        rows.append(row)
    return rows


def _resolve(adapter_config, limits):
    with (
        patch(f"{_MOD}.AdapterConfigRepository") as adapter_repo,
        patch(f"{_MOD}.CurrencyLimitRepository") as limit_repo,
    ):
        adapter_repo.return_value.find_by_tenant.return_value = adapter_config
        limit_repo.return_value.list_all.return_value = limits
        return tenant_reporting_currency(MagicMock(), "tenant_1")


def test_gam_tenant_uses_network_currency_over_currency_limits():
    adapter_config = MagicMock()
    adapter_config.adapter_type = "google_ad_manager"
    adapter_config.gam_network_currency = "EUR"

    assert _resolve(adapter_config, _limits("AUD", "EUR", "USD")) == "EUR"


def test_improvedigital_tenant_uses_office_currency_over_currency_limits():
    adapter_config = MagicMock()
    adapter_config.adapter_type = "improvedigital"
    adapter_config.gam_network_currency = None
    adapter_config.config_json = {"currency": "eur"}

    assert _resolve(adapter_config, _limits("AUD", "EUR", "USD")) == "EUR"


def test_without_ad_server_currency_lowest_limit_code_wins():
    adapter_config = MagicMock()
    adapter_config.adapter_type = "mock"
    adapter_config.gam_network_currency = None
    adapter_config.config_json = {}

    assert _resolve(adapter_config, _limits("USD", "EUR")) == "EUR"


def test_nothing_configured_defaults_to_eur():
    assert _resolve(None, []) == "EUR"
