"""GAM line-item goal booking: budget → primaryGoal.units integrity.

Regression coverage for the production incident where a 10-day €1 buy at
€1 CPM booked a LIFETIME goal of 111 impressions instead of 1000
(order 4111085033): impl_config requested a DAILY goal, so units were
divided by flight days, then the STANDARD branch overwrote the goal type
to LIFETIME without recomputing units. Also covers the booking invariant
(goal × unit price must reconcile with the package budget) and the
per-pricing-model unit conversion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from src.adapters.gam.managers.orders import GAMOrdersManager
from src.core.tools.media_buy_create import _goal_units_from_budget


class _LineItemService:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def createLineItems(self, line_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.created.extend(line_items)
        return [{"id": 55555}]


class _ClientManager:
    def __init__(self, line_item_service: _LineItemService) -> None:
        self.line_item_service = line_item_service

    def get_service(self, service_name: str) -> _LineItemService:
        assert service_name == "LineItemService"
        return self.line_item_service


def _make_manager(service: _LineItemService) -> GAMOrdersManager:
    return GAMOrdersManager(
        client_manager=_ClientManager(service),
        advertiser_id="12345",
        trafficker_id="67890",
        dry_run=False,
    )


def _make_package(impressions: int, budget: float | None) -> SimpleNamespace:
    return SimpleNamespace(
        package_id="pkg_goal",
        product_id="prod_goal",
        name="Goal Package",
        impressions=impressions,
        budget=budget,
        format_ids=[],
        targeting_overlay=None,
        creative_ids=None,
    )


def _products_map(delivery_type: str, impl_config: dict[str, Any]) -> dict[str, Any]:
    return {
        "pkg_goal": {
            "product_id": "prod_goal",
            "delivery_type": delivery_type,
            "implementation_config": {"targeted_ad_unit_ids": ["123456"], **impl_config},
        }
    }


def _pricing(pricing_model: str, rate: float) -> dict[str, dict[str, Any]]:
    return {
        "pkg_goal": {
            "pricing_model": pricing_model,
            "rate": rate,
            "currency": "EUR",
            "is_fixed": True,
            "bid_price": None,
        }
    }


# Flight ends 23:59:59 — 10 calendar days. timedelta.days truncates this
# to 9, which is exactly how the 111-goal incident got its divisor.
START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
END = datetime(2026, 1, 10, 23, 59, 59, tzinfo=UTC)


class TestStandardLifetimeGoal:
    def test_daily_impl_config_ships_full_lifetime_goal(self):
        """STANDARD + impl DAILY must book the FULL goal, not one day's slice.

        Regression: order 4111085033 booked LIFETIME/111 for a €1 @ €1 CPM
        10-day buy because DAILY-divided units survived the LIFETIME overwrite.
        """
        service = _LineItemService()
        manager = _make_manager(service)

        manager.create_line_items(
            order_id="111",
            packages=[_make_package(impressions=1000, budget=1.0)],
            start_time=START,
            end_time=END,
            products_map=_products_map("guaranteed", {"primary_goal_type": "DAILY"}),
            package_pricing_info=_pricing("cpm", 1.0),
        )

        assert service.created[0]["primaryGoal"] == {
            "goalType": "LIFETIME",
            "unitType": "IMPRESSIONS",
            "units": 1000,
        }

    def test_cpc_goal_books_clicks_without_mille_factor(self):
        """CPC goals are click counts: €1 at €0.50 CPC buys 2 clicks."""
        service = _LineItemService()
        manager = _make_manager(service)

        manager.create_line_items(
            order_id="111",
            packages=[_make_package(impressions=2, budget=1.0)],
            start_time=START,
            end_time=END,
            products_map=_products_map("non_guaranteed", {}),
            package_pricing_info=_pricing("cpc", 0.5),
        )

        goal = service.created[0]["primaryGoal"]
        assert goal["unitType"] == "CLICKS"
        assert goal["units"] == 2


class TestDailyGoalFlightDays:
    def test_daily_goal_divides_by_calendar_days_not_truncated_days(self):
        """A 10-day flight ending 23:59:59 divides by 10, not 9."""
        service = _LineItemService()
        manager = _make_manager(service)

        manager.create_line_items(
            order_id="111",
            packages=[_make_package(impressions=1000, budget=1.0)],
            start_time=START,
            end_time=END,
            products_map=_products_map("non_guaranteed", {"primary_goal_type": "DAILY"}),
            package_pricing_info=_pricing("cpm", 1.0),
        )

        goal = service.created[0]["primaryGoal"]
        assert goal["goalType"] == "DAILY"
        assert goal["units"] == 100  # 1000 / 10 days, not 1000 / 9 = 111


class TestBookingInvariant:
    def test_goal_budget_mismatch_refuses_to_book(self):
        """A LIFETIME goal worth a fraction of the budget must not ship."""
        service = _LineItemService()
        manager = _make_manager(service)

        with pytest.raises(ValueError, match="booking mismatch"):
            manager.create_line_items(
                order_id="111",
                packages=[_make_package(impressions=111, budget=1.0)],
                start_time=START,
                end_time=END,
                products_map=_products_map("guaranteed", {}),
                package_pricing_info=_pricing("cpm", 1.0),
            )
        assert service.created == []

    def test_non_positive_goal_refuses_to_book(self):
        service = _LineItemService()
        manager = _make_manager(service)

        with pytest.raises(ValueError, match="non-positive"):
            manager.create_line_items(
                order_id="111",
                packages=[_make_package(impressions=0, budget=1.0)],
                start_time=START,
                end_time=END,
                products_map=_products_map("guaranteed", {}),
                package_pricing_info=_pricing("cpm", 1.0),
            )
        assert service.created == []


class TestGoalUnitsFromBudget:
    def test_cpm_uses_mille(self):
        assert _goal_units_from_budget(1.0, "cpm", 1.0) == 1000

    def test_vcpm_uses_mille(self):
        assert _goal_units_from_budget(500.0, "vcpm", 4.0) == 125_000

    def test_cpc_is_per_unit(self):
        assert _goal_units_from_budget(500.0, "cpc", 0.5) == 1000

    def test_floors_partial_units(self):
        assert _goal_units_from_budget(1.0, "cpm", 3.0) == 333

    def test_zero_or_missing_rate_returns_zero(self):
        assert _goal_units_from_budget(1.0, "cpm", 0.0) == 0
        assert _goal_units_from_budget(1.0, "cpm", None) == 0
