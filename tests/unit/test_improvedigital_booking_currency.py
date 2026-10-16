"""Improve Digital books under one buying-entity office, and the office fixes
the campaign currency. A buy priced in another currency would be trafficked
at face value in the office currency, so create_media_buy refuses it.
"""

from unittest.mock import MagicMock

import pytest

from src.core.schemas import CreateMediaBuyError
from src.core.tools.media_buy_create import _create_media_buy_impl, _improvedigital_office_currency
from tests.unit.test_create_media_buy_behavioral import (
    _make_request,
    _mock_currency_limit,
    _mock_product,
    _PatchContext,
)


def _improvedigital_config(currency: str | None) -> MagicMock:
    adapter_config = MagicMock()
    adapter_config.adapter_type = "improvedigital"
    adapter_config.gam_network_currency = None
    adapter_config.gam_secondary_currencies = None
    adapter_config.config_json = {"currency": currency} if currency else {}
    return adapter_config


def test_office_currency_is_read_from_config_json():
    assert _improvedigital_office_currency(_improvedigital_config(" eur ")) == "EUR"
    assert _improvedigital_office_currency(_improvedigital_config(None)) is None
    assert _improvedigital_office_currency(None) is None


@pytest.mark.asyncio
async def test_currency_other_than_the_office_currency_is_rejected():
    req = _make_request(packages=[{"product_id": "prod_1", "budget": 5000.0, "pricing_option_id": "cpm_usd_fixed"}])
    product = _mock_product("prod_1", currency="USD")

    with _PatchContext(
        products=[product], currency_limit=_mock_currency_limit(), adapter_config=_improvedigital_config("EUR")
    ) as pc:
        result = await _create_media_buy_impl(req=req, identity=pc.identity)

    assert isinstance(result.response, CreateMediaBuyError)
    message = result.response.errors[0].message
    assert "USD is not supported" in message
    assert "Improve Digital" in message
    assert "books in EUR" in message
