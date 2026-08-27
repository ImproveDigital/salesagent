"""Live-mode delivery reads of the Improve Digital adapter (mocked cache).

``get_media_buy_delivery`` aggregates the ``improvedigital_line_item_stats``
cache; an empty cache raises ``DeliveryDataUnavailable`` (never fake zeros),
and internal ``mb_*`` buy references resolve their Classic campaign via the
packages' ``platform_order_id`` (stamped by the core layer at create).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.adapters.base import DeliveryDataUnavailable
from src.core.schemas import ReportingPeriod
from tests.unit.adapters.improvedigital.test_live_booking import make_live_adapter

pytestmark = pytest.mark.unit


def _reporting_period() -> ReportingPeriod:
    start = datetime.now(UTC) - timedelta(days=7)
    return ReportingPeriod(start=start, end=start + timedelta(days=7))


def _stat_row(line_item_id: str, impressions: int, spend_micros: int, completed_views: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        line_item_id=line_item_id,
        impressions=impressions,
        clicks=10,
        completed_views=completed_views,
        spend_micros=spend_micros,
        currency="EUR",
    )


@contextmanager
def patched_delivery(
    stat_rows: list[SimpleNamespace], buy: SimpleNamespace | None = None, packages: list | None = None
):
    """Patch the DB session + repositories the delivery read path opens.

    ``buy``/``packages`` are what ``MediaBuyRepository.get_by_id`` /
    ``get_packages`` return (only consulted for internal ``mb_*``
    references); ``stat_rows`` back
    ``ImproveDigitalLineItemStatsRepository.list_by_campaign``.
    """
    with (
        patch("src.core.database.database_session.get_db_session"),
        patch("src.core.database.repositories.media_buy.MediaBuyRepository") as buy_repo_cls,
        patch(
            "src.core.database.repositories.improvedigital_line_item_stats.ImproveDigitalLineItemStatsRepository"
        ) as stats_repo_cls,
    ):
        buy_repo_cls.return_value.get_by_id.return_value = buy
        buy_repo_cls.return_value.get_packages.return_value = packages or []
        stats_repo_cls.return_value.list_by_campaign.return_value = stat_rows
        yield stats_repo_cls.return_value


class TestDeliveryLive:
    def test_empty_cache_raises_delivery_unavailable(self):
        adapter = make_live_adapter()
        with patched_delivery([]):
            with pytest.raises(DeliveryDataUnavailable):
                adapter.get_media_buy_delivery("improvedigital_101", _reporting_period(), datetime.now(UTC))

    def test_internal_media_buy_id_resolves_campaign_via_external_id(self):
        """Callers (admin delivery sync, MCP delivery tool) pass the core
        layer's internal ``mb_*`` ID; the Classic campaign reference lives on
        ``media_buys.external_id``. The read path must resolve it the same
        way the reporting sync's write path does (platform_order_id)."""
        adapter = make_live_adapter()
        rows = [_stat_row("202", impressions=1000, spend_micros=4_000_000, completed_views=None)]
        buy = SimpleNamespace(media_buy_id="mb_70aa67ac413f")
        packages = [SimpleNamespace(package_config={"platform_order_id": "improvedigital_101"})]
        with patched_delivery(rows, buy=buy, packages=packages) as stats_repo:
            response = adapter.get_media_buy_delivery("mb_70aa67ac413f", _reporting_period(), datetime.now(UTC))
        stats_repo.list_by_campaign.assert_called_once_with("101")
        assert response.totals.impressions == 1000

    def test_internal_id_without_external_mapping_soft_fails(self):
        """An internal ID whose buy row is missing (or has no external stamp)
        must stay a soft DeliveryDataUnavailable, never a hard error."""
        adapter = make_live_adapter()
        with patched_delivery([], buy=None):
            with pytest.raises(DeliveryDataUnavailable):
                adapter.get_media_buy_delivery("mb_70aa67ac413f", _reporting_period(), datetime.now(UTC))

    def test_cache_rows_aggregate_to_delivery_totals(self):
        adapter = make_live_adapter()
        rows = [
            _stat_row("202", impressions=1000, spend_micros=4_000_000, completed_views=700),
            _stat_row("203", impressions=500, spend_micros=2_000_000, completed_views=100),
        ]
        with patched_delivery(rows):
            response = adapter.get_media_buy_delivery("improvedigital_101", _reporting_period(), datetime.now(UTC))
        assert response.totals.impressions == 1500
        assert response.totals.spend == 6.0
        assert {p.package_id for p in response.by_package} == {"202", "203"}
        assert response.currency == "EUR"
