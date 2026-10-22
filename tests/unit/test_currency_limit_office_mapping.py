"""Improve Digital offices live on the currency limits, one office per currency.

The Policies page maps each currency to the buying-entity office that bills
in it; booking resolves the office from the buy currency and sends it with
the campaign; no office means the currency cannot be booked.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.admin.blueprints.settings import parse_form_data_to_policy_updates
from src.core.database.repositories.currency_limit import CurrencyLimitRepository
from src.core.schemas import CreateMediaBuyError
from src.services.policy_service import CurrencyLimitData, PolicyService, ValidationError
from tests.helpers.adapter_test_helpers import invoke_create_media_buy, make_sample_create_request
from tests.unit.test_improvedigital_live_paths import make_live_adapter, make_targeted_package

# --- policy service -----------------------------------------------------------------


def test_form_parser_reads_the_office_per_currency():
    form = {
        "currency_limits[EUR][min_package_budget]": "10",
        "currency_limits[EUR][buying_entity_office_id]": "5068",
        "currency_limits[USD][buying_entity_office_id]": "",
        "currency_limits[USD][_delete]": "false",
    }

    currencies = {c.currency_code: c for c in parse_form_data_to_policy_updates(form)["currencies"]}

    assert currencies["EUR"].buying_entity_office_id == 5068
    assert currencies["EUR"].min_package_budget == Decimal("10")
    assert currencies["USD"].buying_entity_office_id is None


def test_one_office_cannot_bill_two_currencies():
    with pytest.raises(ValidationError) as exc:
        PolicyService.validate_currency_limits(
            [
                CurrencyLimitData(currency_code="EUR", buying_entity_office_id=5068),
                CurrencyLimitData(currency_code="USD", buying_entity_office_id=5068),
            ]
        )

    assert "USD_office" in exc.value.errors
    assert "already mapped to EUR" in exc.value.errors["USD_office"]


def test_office_id_must_be_positive_and_distinct_offices_are_fine():
    with pytest.raises(ValidationError) as exc:
        PolicyService.validate_currency_limits([CurrencyLimitData(currency_code="EUR", buying_entity_office_id=0)])
    assert "EUR_office" in exc.value.errors

    PolicyService.validate_currency_limits(
        [
            CurrencyLimitData(currency_code="EUR", buying_entity_office_id=5068),
            CurrencyLimitData(currency_code="USD", buying_entity_office_id=5069),
            CurrencyLimitData(currency_code="GBP"),
        ]
    )


def test_update_writes_the_office_on_new_and_existing_rows():
    existing = MagicMock()
    existing.currency_code = "EUR"
    session = MagicMock()
    session.scalars.return_value.all.return_value = [existing]

    PolicyService._update_currencies(
        session,
        "t1",
        [
            CurrencyLimitData(currency_code="EUR", buying_entity_office_id=5068),
            CurrencyLimitData(currency_code="USD", buying_entity_office_id=5069),
        ],
    )

    assert existing.buying_entity_office_id == 5068
    created = session.add.call_args.args[0]
    assert (created.currency_code, created.buying_entity_office_id) == ("USD", 5069)


# --- repository ---------------------------------------------------------------------


def test_repository_resolves_office_from_the_currency_row():
    session = MagicMock()
    row = MagicMock()
    row.buying_entity_office_id = 5068
    session.scalars.return_value.first.return_value = row
    repo = CurrencyLimitRepository(session, "t1")

    assert repo.office_for_currency(" eur ") == 5068

    session.scalars.return_value.first.return_value = None
    assert repo.office_for_currency("USD") is None


# --- adapter booking ----------------------------------------------------------------


def _book(adapter, office_id, currency="USD"):
    pricing = {"pkg_video_1": {"pricing_model": "cpm", "rate": 4.0, "currency": currency, "is_fixed": True}}
    with patch.object(adapter, "_office_for_currency", return_value=office_id) as resolve:
        response = invoke_create_media_buy(
            adapter, make_sample_create_request(), [make_targeted_package()], package_pricing_info=pricing
        )
    return response, resolve


def test_booking_sends_the_office_mapped_to_the_buy_currency():
    adapter = make_live_adapter()  # default currency EUR; the buy is USD

    response, resolve = _book(adapter, 5069, currency="USD")

    resolve.assert_called_once_with("USD")
    assert not isinstance(response, CreateMediaBuyError), getattr(response, "errors", None)
    campaign_payload = next(call[1] for call in adapter._client.campaigns.calls if call[0] == "create_campaign")
    assert campaign_payload["currency"] == "USD"
    assert campaign_payload["buying_entity_office_id"] == 5069
    assert campaign_payload["buying_entity_office_ids"] == [5069]


def test_booking_without_a_mapped_office_is_refused_before_any_api_call():
    adapter = make_live_adapter()

    response, _ = _book(adapter, None, currency="USD")

    assert isinstance(response, CreateMediaBuyError)
    assert response.errors[0].code == "currency_not_bookable"
    assert "USD" in response.errors[0].message
    assert adapter._client.campaigns.calls == []


def test_booking_currency_falls_back_to_the_default_when_no_pricing_info():
    adapter = make_live_adapter()

    assert adapter._booking_currency(None) == "EUR"
    assert adapter._booking_currency({"pkg": {"currency": "gbp"}}) == "GBP"
