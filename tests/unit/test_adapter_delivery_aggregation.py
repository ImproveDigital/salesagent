"""_aggregate_stat_rows_to_delivery_response must carry every metric the
stats cache holds.

Regression: the helper populated impressions/spend/completed_views but
dropped clicks entirely, so the media buy details page (which reads
``totals.clicks`` / ``totals.ctr`` via adapter.get_media_buy_delivery)
showed empty click metrics while the reporting page — reading the same
cache rows directly — showed real ones.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from src.adapters.base import AdServerAdapter
from src.core.schemas import ReportingPeriod


def make_row(line_item_id: str, impressions: int, clicks: int | None, spend_micros: int, completed: int | None):
    return SimpleNamespace(
        line_item_id=line_item_id,
        impressions=impressions,
        clicks=clicks,
        spend_micros=spend_micros,
        completed_views=completed,
        currency="EUR",
    )


PERIOD = ReportingPeriod(start=datetime(2026, 8, 1, tzinfo=UTC), end=datetime(2026, 8, 18, tzinfo=UTC))


class TestAggregateStatRows:
    def test_clicks_and_ctr_are_aggregated(self):
        rows = [
            make_row("li1", 28287, 30, 25_277_857, 277),
            make_row("li2", 934, 0, 604_944, 20),
        ]
        response = AdServerAdapter._aggregate_stat_rows_to_delivery_response(
            "improvedigital_314446", PERIOD, rows, package_id_attr="line_item_id", default_currency="EUR"
        )
        assert response.totals.impressions == 29221.0
        assert response.totals.clicks == 30.0
        assert response.totals.ctr == 30 / 29221
        assert round(response.totals.spend, 2) == 25.88
        assert response.totals.completed_views == 297.0
        assert response.currency == "EUR"

    def test_ctr_clamps_when_clicks_exceed_impressions(self):
        """Click trackers/companion clicks can report clicks > impressions;
        DeliveryTotals.ctr enforces le=1, so the ratio must clamp instead of
        raising ValidationError and killing the delivery response."""
        rows = [make_row("li1", 3, 5, 0, None)]
        response = AdServerAdapter._aggregate_stat_rows_to_delivery_response(
            "buy", PERIOD, rows, package_id_attr="line_item_id"
        )
        assert response.totals.clicks == 5.0
        assert response.totals.ctr == 1.0

    def test_clicks_stay_none_when_platform_reports_none(self):
        rows = [make_row("li1", 100, None, 0, None)]
        response = AdServerAdapter._aggregate_stat_rows_to_delivery_response(
            "buy", PERIOD, rows, package_id_attr="line_item_id"
        )
        assert response.totals.clicks is None
        assert response.totals.ctr is None
