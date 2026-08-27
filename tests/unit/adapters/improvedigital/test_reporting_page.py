"""Payload shaping for the Improve Digital admin reporting page.

Covers ``_improvedigital_reporting_payload`` — the pure transform between
``improvedigital_line_item_stats`` cache rows and the JSON the reporting
template renders. No Flask, no DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.admin.blueprints.adapters import _improvedigital_reporting_payload

pytestmark = pytest.mark.unit

AS_OF = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)


def _stat(line_item_id="202", campaign_id="555103", impressions=1000, clicks=10, completed=None, micros=4_000_000):
    return SimpleNamespace(
        line_item_id=line_item_id,
        campaign_id=campaign_id,
        impressions=impressions,
        clicks=clicks,
        completed_views=completed,
        spend_micros=micros,
        currency="EUR",
        as_of=AS_OF,
    )


def _buy(media_buy_id="mb_1", order_name="Summer Push", advertiser_name="Brand X"):
    return SimpleNamespace(media_buy_id=media_buy_id, order_name=order_name, advertiser_name=advertiser_name)


class TestReportingPayload:
    def test_rows_join_media_buys_and_convert_micros(self):
        payload = _improvedigital_reporting_payload([_stat()], {"555103": _buy()}, "EUR")

        row = payload["rows"][0]
        assert row["media_buy_id"] == "mb_1"
        assert row["order_name"] == "Summer Push"
        assert row["advertiser_name"] == "Brand X"
        assert row["spend"] == 4.0  # micros → currency units
        assert row["ctr"] == 1.0  # 10 clicks / 1000 impressions
        assert row["as_of"] == AS_OF.isoformat()

    def test_unattributed_campaign_still_renders(self):
        # Campaigns booked outside salesagent have no matching media buy —
        # the row must render unattributed, never be dropped.
        payload = _improvedigital_reporting_payload([_stat(campaign_id="999")], {}, "EUR")

        row = payload["rows"][0]
        assert row["media_buy_id"] is None
        assert row["order_name"] is None
        assert row["impressions"] == 1000

    def test_totals_aggregate_across_rows(self):
        stats = [
            _stat(line_item_id="202", impressions=1000, clicks=10, micros=4_000_000),
            _stat(line_item_id="203", impressions=500, clicks=None, completed=100, micros=2_000_000),
        ]
        payload = _improvedigital_reporting_payload(stats, {}, "EUR")

        totals = payload["totals"]
        assert totals["impressions"] == 1500
        assert totals["clicks"] == 10
        assert totals["ctr"] == round(10 / 1500 * 100, 2)
        assert totals["completed_views"] == 100
        assert totals["spend"] == 6.0

    def test_zero_impressions_yields_null_ctr(self):
        payload = _improvedigital_reporting_payload([_stat(impressions=0, clicks=0, micros=0)], {}, "EUR")

        assert payload["rows"][0]["ctr"] is None
        assert payload["totals"]["ctr"] is None
        assert payload["totals"]["spend"] == 0.0
