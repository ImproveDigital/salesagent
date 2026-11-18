"""Unit tests for PR04 financial guardrails: F-05, F-07, F-08.

F-05 — Budget ceiling: updates exceeding MAX_CAMPAIGN_BUDGET are rejected.
F-07 — Currency preservation: float-only budget updates use existing DB currency.
F-08 — Min-spend parity: package budget updates honor currency_limit.min_package_budget.
       NOTE: this check is currently commented out in production ("minimum package
       budget validation temporarily disabled", media_buy_update.py, since 99ad84c4),
       so the F-08 test below pins the disabled behaviour until it is re-enabled.
"""

from decimal import Decimal

from src.core.schemas import Budget, UpdateMediaBuyError, UpdateMediaBuySuccess
from src.core.tools.media_buy_update import MAX_CAMPAIGN_BUDGET
from tests.harness.media_buy_update import MediaBuyUpdateEnv

# ---------------------------------------------------------------------------
# F-05: Budget ceiling
# ---------------------------------------------------------------------------


def test_max_campaign_budget_constant_is_ten_million() -> None:
    """Default ceiling must be 10,000,000."""
    assert MAX_CAMPAIGN_BUDGET == Decimal("10000000")


def test_extreme_budget_rejected() -> None:
    """Budget exceeding MAX_CAMPAIGN_BUDGET must return UpdateMediaBuyError."""
    with MediaBuyUpdateEnv() as env:
        result = env.call_impl(budget=Budget(total=888_888_888, currency="USD"))

    assert isinstance(result, UpdateMediaBuyError)
    assert result.errors
    assert result.errors[0].code == "budget_ceiling_exceeded"


# ---------------------------------------------------------------------------
# F-05: Constant is configurable via env var
# ---------------------------------------------------------------------------


def test_max_campaign_budget_env_override(monkeypatch) -> None:
    """MAX_CAMPAIGN_BUDGET should reflect MAX_CAMPAIGN_BUDGET_USD env var."""
    import importlib

    monkeypatch.setenv("MAX_CAMPAIGN_BUDGET_USD", "5000000")
    import src.core.tools.media_buy_update as mod

    importlib.reload(mod)
    assert mod.MAX_CAMPAIGN_BUDGET == Decimal("5000000")
    # Restore
    importlib.reload(mod)


# ---------------------------------------------------------------------------
# F-08: Min-spend parity via CurrencyLimitRepository
# ---------------------------------------------------------------------------


def test_package_budget_below_min_spend_not_rejected_while_check_disabled() -> None:
    """A package budget below currency_limit.min_package_budget is currently accepted.

    The F-08 min-spend check in ``_update_media_buy_impl`` is commented out in
    production ("minimum package budget validation temporarily disabled",
    introduced in 99ad84c4 / Release v1.1.5, mirrored in media_buy_create.py).
    Until it is re-enabled the update goes straight to the adapter.

    When the check is restored this test must go back to expecting
    ``UpdateMediaBuyError`` with code ``budget_below_minimum`` and
    ``uow.currency_limits.get_for_currency`` being called with the media buy's
    currency ("EUR").
    """
    with MediaBuyUpdateEnv() as env:
        env.set_media_buy(currency="EUR")
        env.set_currency_limit(min_package_budget=Decimal("100"))
        adapter = env.mock["adapter"].return_value
        adapter.update_media_buy.return_value = UpdateMediaBuySuccess(media_buy_id="mb-001", affected_packages=[])

        result = env.call_impl(packages=[{"package_id": "pkg-1", "budget": 50.0}])

        # Still no raw session selects on this path.
        env.mock["uow"].return_value.session.scalars.assert_not_called()
        budget_call = adapter.update_media_buy.call_args

    assert isinstance(result, UpdateMediaBuySuccess)
    assert budget_call.kwargs["action"] == "update_package_budget"
    assert budget_call.kwargs["package_id"] == "pkg-1"
    assert budget_call.kwargs["budget"] == 50
