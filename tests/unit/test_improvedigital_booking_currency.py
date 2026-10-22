"""Improve Digital books each Classic campaign under the buying-entity office
that bills in the buy currency (one office per currency, mapped on the
tenant's currency limits). A currency with no mapped office cannot be
trafficked, so create_media_buy refuses it before anything reaches the
adapter.
"""

from unittest.mock import MagicMock

import pytest

from src.core.schemas import CreateMediaBuyError
from src.core.tools.media_buy_create import _create_media_buy_impl
from tests.unit.test_create_media_buy_behavioral import (
    _make_request,
    _mock_currency_limit,
    _mock_product,
    _PatchContext,
)


def _improvedigital_config() -> MagicMock:
    adapter_config = MagicMock()
    adapter_config.adapter_type = "improvedigital"
    adapter_config.gam_network_currency = None
    adapter_config.gam_secondary_currencies = None
    adapter_config.config_json = {"currency": "EUR", "buying_entity_id": 421}
    return adapter_config


def _limit(office_id: int | None) -> MagicMock:
    limit = _mock_currency_limit()
    limit.buying_entity_office_id = office_id
    return limit


@pytest.mark.asyncio
async def test_currency_without_a_mapped_office_is_rejected():
    req = _make_request(packages=[{"product_id": "prod_1", "budget": 5000.0, "pricing_option_id": "cpm_usd_fixed"}])
    product = _mock_product("prod_1", currency="USD")

    with _PatchContext(products=[product], currency_limit=_limit(None), adapter_config=_improvedigital_config()) as pc:
        result = await _create_media_buy_impl(req=req, identity=pc.identity)

    assert isinstance(result.response, CreateMediaBuyError)
    message = result.response.errors[0].message
    assert "USD has no Improve Digital buying entity office mapped" in message
    assert "Budget Controls" in message
