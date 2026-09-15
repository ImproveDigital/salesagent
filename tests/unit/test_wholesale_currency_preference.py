"""Wholesale bundle pricing must honour the ad server's configured currency.

Inventory bundles carry no pricing options of their own; get_products projects
each one into a single auction-CPM option in a tenant-wide default currency.
That currency has to follow the adapter's configured currency (GAM network
currency, or ``config_json["currency"]`` for Improve Digital and friends),
otherwise buyers are quoted a currency the ad server will never book in.
"""

from src.core.database.models import AdapterConfig, CurrencyLimit, InventoryProfile
from src.core.inventory_profile_projection import (
    default_wholesale_currency,
    inventory_profile_to_product_model,
    preferred_wholesale_currency,
)


def _limits(*codes: str) -> list[CurrencyLimit]:
    return [CurrencyLimit(tenant_id="t", currency_code=code) for code in codes]


def test_improvedigital_config_currency_is_preferred():
    adapter = AdapterConfig(tenant_id="t", adapter_type="improvedigital", config_json={"currency": "EUR"})

    assert preferred_wholesale_currency(adapter) == "EUR"
    assert default_wholesale_currency(_limits("USD", "EUR"), preferred=preferred_wholesale_currency(adapter)) == "EUR"


def test_gam_network_currency_is_preferred_over_config_json():
    adapter = AdapterConfig(
        tenant_id="t",
        adapter_type="google_ad_manager",
        gam_network_currency="GBP",
        config_json={"currency": "EUR"},
    )

    assert preferred_wholesale_currency(adapter) == "GBP"


def test_no_configured_currency_falls_back_to_currency_limits():
    assert preferred_wholesale_currency(None) is None
    assert preferred_wholesale_currency(AdapterConfig(tenant_id="t", adapter_type="mock", config_json={})) is None
    assert preferred_wholesale_currency(AdapterConfig(tenant_id="t", adapter_type="mock", config_json=None)) is None
    assert default_wholesale_currency(_limits("USD", "EUR"), preferred=None) == "USD"


def test_configured_currency_without_matching_limit_falls_back():
    adapter = AdapterConfig(tenant_id="t", adapter_type="improvedigital", config_json={"currency": "GBP"})

    assert default_wholesale_currency(_limits("USD", "EUR"), preferred=preferred_wholesale_currency(adapter)) == "USD"


def test_projected_bundle_pricing_option_uses_preferred_currency():
    profile = InventoryProfile(
        tenant_id="t",
        profile_id="improve_test_bundle",
        name="Improve Test Bundle",
        inventory_config={"ad_units": [], "placements": [], "include_descendants": False},
        format_ids=[{"agent_url": "https://creative.adcontextprotocol.org/", "id": "display_html"}],
        publisher_properties=[
            {"publisher_domain": "spel.nl", "selection_type": "by_tag", "property_tags": ["all_inventory"]}
        ],
    )
    adapter = AdapterConfig(tenant_id="t", adapter_type="improvedigital", config_json={"currency": "eur"})
    currency = default_wholesale_currency(_limits("USD", "EUR"), preferred=preferred_wholesale_currency(adapter))

    product = inventory_profile_to_product_model(profile, default_currency=currency)

    assert [po.currency for po in product.pricing_options] == ["EUR"]
