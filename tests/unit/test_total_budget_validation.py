"""Regression tests: create_media_buy must validate total_budget (FMS-2161).

total_budget was accepted by the schema but never read, so a USD total_budget
on an EUR-priced buy succeeded and was stored as EUR.
"""

from types import SimpleNamespace

import pytest

from src.core.tools.media_buy_create import _validate_total_budget


def _req(amount=None, currency=None, mode=None):
    total_budget = None if amount is None else SimpleNamespace(amount=amount, currency=currency)
    allocation = None if mode is None else SimpleNamespace(mode=mode)
    return SimpleNamespace(total_budget=total_budget, budget_allocation=allocation)


def test_absent_total_budget_is_accepted():
    _validate_total_budget(_req(), "EUR", 1.0)


def test_matching_total_budget_is_accepted():
    _validate_total_budget(_req(1, "EUR"), "EUR", 1.0)


def test_currency_mismatch_is_rejected():
    with pytest.raises(ValueError, match="total_budget.currency USD does not match"):
        _validate_total_budget(_req(1, "USD"), "EUR", 1.0)


def test_amount_not_equal_to_package_sum_is_rejected():
    with pytest.raises(ValueError, match="does not equal the sum of package budgets"):
        _validate_total_budget(_req(100, "EUR"), "EUR", 1.0)


def test_seller_optimized_skips_sum_check_but_not_currency():
    _validate_total_budget(_req(100, "EUR", mode="seller_optimized"), "EUR", 1.0)
    with pytest.raises(ValueError, match="total_budget.currency"):
        _validate_total_budget(_req(100, "USD", mode="seller_optimized"), "EUR", 1.0)
