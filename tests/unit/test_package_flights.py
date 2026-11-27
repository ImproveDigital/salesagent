"""A product can back several packages in one media buy when each has its own flight.

Buyers book back-to-back flights of the same inventory as two packages on one
product. The duplicate-product rule only refuses packages that share both the
product and the flight, and each package's flight reaches its own GAM line item.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from src.adapters.gam.managers.orders import GAMOrdersManager
from src.core.tools.media_buy_create import (
    _duplicate_product_ids,
    _own_package_flight,
    _package_flight_config,
)

BUY_START = datetime(2026, 11, 26, 21, 0, tzinfo=UTC)
BUY_END = datetime(2026, 11, 29, 21, 0, tzinfo=UTC)
FIRST_END = BUY_START + timedelta(hours=1)
SECOND_START = FIRST_END + timedelta(minutes=5)


def _package(product_id: str = "prod_1", start: datetime | None = None, end: datetime | None = None):
    return SimpleNamespace(product_id=product_id, start_time=start, end_time=end)


class TestDuplicateProductRule:
    def test_same_product_with_different_flights_is_allowed(self):
        packages = [_package(start=BUY_START, end=FIRST_END), _package(start=SECOND_START, end=BUY_END)]

        assert _duplicate_product_ids(packages) == []

    def test_same_product_without_package_flights_is_a_duplicate(self):
        assert _duplicate_product_ids([_package(), _package()]) == ["prod_1"]

    def test_same_product_with_the_same_flight_is_a_duplicate(self):
        packages = [_package(start=BUY_START, end=FIRST_END), _package(start=BUY_START, end=FIRST_END)]

        assert _duplicate_product_ids(packages) == ["prod_1"]

    def test_each_duplicated_product_is_listed_once(self):
        packages = [_package("prod_1"), _package("prod_1"), _package("prod_1"), _package("prod_2"), _package("prod_2")]

        assert _duplicate_product_ids(packages) == ["prod_1", "prod_2"]

    def test_different_products_are_not_duplicates(self):
        assert _duplicate_product_ids([_package("prod_1"), _package("prod_2")]) == []


class TestOwnPackageFlight:
    def test_package_without_dates_runs_for_the_whole_buy(self):
        assert _own_package_flight(_package(), BUY_START, BUY_END) == (None, None)

    def test_package_dates_inside_the_buy_are_kept(self):
        assert _own_package_flight(_package(start=SECOND_START, end=BUY_END), BUY_START, BUY_END) == (
            SECOND_START,
            BUY_END,
        )

    def test_start_before_the_buy_is_moved_up_to_the_buy_start(self):
        """A buyer sending "now" on an asap buy is always a moment behind our own "now"."""
        early = BUY_START - timedelta(seconds=30)

        assert _own_package_flight(_package(start=early, end=FIRST_END), BUY_START, BUY_END) == (BUY_START, FIRST_END)

    def test_missing_end_defaults_to_the_buy_end(self):
        assert _own_package_flight(_package(start=SECOND_START), BUY_START, BUY_END) == (SECOND_START, BUY_END)

    def test_package_ending_after_the_buy_is_rejected(self):
        with pytest.raises(ValueError, match="after the media buy ends"):
            _own_package_flight(_package(start=BUY_START, end=BUY_END + timedelta(hours=1)), BUY_START, BUY_END)

    def test_package_that_does_not_end_after_it_starts_is_rejected(self):
        with pytest.raises(ValueError, match="must end"):
            _own_package_flight(_package(start=SECOND_START, end=FIRST_END), BUY_START, BUY_END)

    def test_stored_config_carries_the_flight_as_iso_strings(self):
        config = _package_flight_config(_package(start=SECOND_START, end=BUY_END), BUY_START, BUY_END)

        assert config == {"start_time": SECOND_START.isoformat(), "end_time": BUY_END.isoformat()}

    def test_stored_config_is_empty_without_package_dates(self):
        assert _package_flight_config(_package(), BUY_START, BUY_END) == {}


class _LineItemService:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def createLineItems(self, line_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.created.extend(line_items)
        return [{"id": 98000 + len(self.created)}]


class _ClientManager:
    def __init__(self, line_item_service: _LineItemService) -> None:
        self.line_item_service = line_item_service

    def get_service(self, service_name: str) -> _LineItemService:
        assert service_name == "LineItemService"
        return self.line_item_service


def _gam_package(package_id: str, start: datetime | None, end: datetime | None):
    return SimpleNamespace(
        package_id=package_id,
        product_id="prod_1",
        name="Gaming audience",
        impressions=0,
        format_ids=[],
        targeting_overlay=None,
        creative_ids=None,
        start_time=start,
        end_time=end,
    )


def _create_line_items(packages: list[Any]) -> list[dict[str, Any]]:
    service = _LineItemService()
    manager = GAMOrdersManager(
        client_manager=_ClientManager(service),
        advertiser_id="12345",
        trafficker_id="67890",
        dry_run=False,
    )
    manager.create_line_items(
        order_id="111",
        packages=packages,
        start_time=BUY_START,
        end_time=BUY_END,
        products_map={
            package.package_id: {
                "product_id": "prod_1",
                "delivery_type": "guaranteed",
                "implementation_config": {"targeted_ad_unit_ids": ["123456"]},
            }
            for package in packages
        },
        package_pricing_info={
            package.package_id: {
                "pricing_model": "cpm",
                "rate": 0.0,
                "currency": "EUR",
                "is_fixed": True,
                "bid_price": None,
                "sandbox_trafficking": True,
            }
            for package in packages
        },
    )
    return service.created


def _hour(gam_datetime: dict[str, Any]) -> tuple[int, int, int]:
    return (gam_datetime["date"]["day"], gam_datetime["hour"], gam_datetime["minute"])


class TestGamLineItemsForPackagesOnOneProduct:
    def test_each_package_flight_becomes_its_line_item_flight(self):
        first, second = _create_line_items(
            [_gam_package("pkg_1", BUY_START, FIRST_END), _gam_package("pkg_2", SECOND_START, BUY_END)]
        )

        assert first["startDateTime"] != second["startDateTime"]
        assert first["endDateTime"] != second["endDateTime"]
        # The first package ends before the second starts.
        assert _hour(first["endDateTime"]) < _hour(second["startDateTime"])

    def test_package_without_dates_uses_the_order_flight(self):
        (with_dates, without_dates) = _create_line_items(
            [_gam_package("pkg_1", BUY_START, FIRST_END), _gam_package("pkg_2", None, None)]
        )

        assert _hour(without_dates["endDateTime"]) > _hour(with_dates["endDateTime"])

    def test_line_item_names_are_distinct_within_the_order(self):
        first, second = _create_line_items(
            [_gam_package("pkg_1", BUY_START, FIRST_END), _gam_package("pkg_2", SECOND_START, BUY_END)]
        )

        assert first["name"] != second["name"]
        assert second["name"].endswith("#2")
