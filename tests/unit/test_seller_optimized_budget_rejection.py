"""Regression tests: create_media_buy must reject seller_optimized budget allocation (FMS-2160).

This seller does not advertise ``media_buy.features.seller_optimized_budget``.
Per AdCP, such sellers MUST reject ``budget_allocation.mode: "seller_optimized"``
with UNSUPPORTED_FEATURE before any provider mutation and MUST NOT coerce the
request to fixed allocation. Previously the mode was silently ignored and the
buy was booked as fixed with the package-budget sum.
"""

import pytest

from src.core.exceptions import AdCPUnsupportedFeatureError
from src.core.schemas import CreateMediaBuyRequest
from src.core.tools.media_buy_create import _reject_unsupported_budget_allocation


def _request(budget_allocation: dict | None) -> CreateMediaBuyRequest:
    kwargs: dict = {
        "idempotency_key": "fms2160-seller-optimized",
        "account": {"account_id": "acct_test"},
        "brand": {"domain": "example.com"},
        "start_time": "2026-12-01T00:00:00Z",
        "end_time": "2026-12-02T00:00:00Z",
        "total_budget": {"amount": 50, "currency": "EUR"},
        "packages": [{"product_id": "prod_1", "pricing_option_id": "cpm_eur_fixed", "budget": 1}],
    }
    if budget_allocation is not None:
        kwargs["budget_allocation"] = budget_allocation
    return CreateMediaBuyRequest(**kwargs)


def test_seller_optimized_is_rejected_with_unsupported_feature():
    req = _request({"mode": "seller_optimized", "optimization_goals": [{"kind": "metric", "metric": "clicks"}]})

    with pytest.raises(AdCPUnsupportedFeatureError) as exc_info:
        _reject_unsupported_budget_allocation(req)

    assert exc_info.value.error_code == "UNSUPPORTED_FEATURE"
    assert exc_info.value.recovery == "correctable"
    assert exc_info.value.details == {"field": "budget_allocation.mode"}
    assert "seller_optimized" in exc_info.value.message


def test_fixed_allocation_is_accepted():
    _reject_unsupported_budget_allocation(_request({"mode": "fixed"}))


def test_omitted_allocation_is_accepted():
    _reject_unsupported_budget_allocation(_request(None))
