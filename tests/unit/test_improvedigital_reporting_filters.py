"""Filtering of the Improve Digital reporting page payload.

The reporting endpoint accepts ``campaign_id`` / ``media_buy_id`` (exact)
and ``q`` (case-insensitive substring across order name, advertiser and
the campaign/line-item/media-buy ids). Filters apply before totals, so
the summary cards always match the visible table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from src.admin.blueprints.adapters import _improvedigital_reporting_payload

AS_OF = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def stat(campaign_id, line_item_id, impressions, clicks, spend_micros):
    return SimpleNamespace(
        campaign_id=campaign_id,
        line_item_id=line_item_id,
        impressions=impressions,
        clicks=clicks,
        completed_views=None,
        spend_micros=spend_micros,
        currency="EUR",
        as_of=AS_OF,
    )


def buy(media_buy_id, order_name, advertiser_name):
    return SimpleNamespace(media_buy_id=media_buy_id, order_name=order_name, advertiser_name=advertiser_name)


STATS = [
    stat("314446", "582321", 28287, 30, 25_277_857),
    stat("314450", "582325", 934, 0, 604_944),
    stat("314455", "585239", 500, 5, 1_250_000),
]
BUYS = {
    "314446": buy("mb_aaa", "Acme Summer Push", "Acme Corp"),
    "314450": buy("mb_bbb", "Globex Launch", "Globex"),
}


class TestReportingFilters:
    def test_unfiltered_keeps_all_rows(self):
        payload = _improvedigital_reporting_payload(STATS, BUYS, "EUR")
        assert len(payload["rows"]) == 3
        assert payload["totals"]["impressions"] == 28287 + 934 + 500

    def test_campaign_id_exact_match(self):
        payload = _improvedigital_reporting_payload(STATS, BUYS, "EUR", campaign_id="314450")
        assert [r["line_item_id"] for r in payload["rows"]] == ["582325"]
        assert payload["totals"]["impressions"] == 934
        assert payload["totals"]["spend"] == 0.6

    def test_media_buy_id_exact_match(self):
        payload = _improvedigital_reporting_payload(STATS, BUYS, "EUR", media_buy_id="mb_aaa")
        assert [r["campaign_id"] for r in payload["rows"]] == ["314446"]
        assert payload["totals"]["clicks"] == 30

    def test_q_matches_order_and_advertiser_case_insensitively(self):
        payload = _improvedigital_reporting_payload(STATS, BUYS, "EUR", q="globex")
        assert [r["campaign_id"] for r in payload["rows"]] == ["314450"]

        payload = _improvedigital_reporting_payload(STATS, BUYS, "EUR", q="ACME")
        assert [r["campaign_id"] for r in payload["rows"]] == ["314446"]

    def test_q_matches_ids_including_unattributed_rows(self):
        payload = _improvedigital_reporting_payload(STATS, BUYS, "EUR", q="585239")
        assert [r["campaign_id"] for r in payload["rows"]] == ["314455"]
        assert payload["rows"][0]["media_buy_id"] is None

    def test_filters_compose(self):
        payload = _improvedigital_reporting_payload(STATS, BUYS, "EUR", campaign_id="314446", q="globex")
        assert payload["rows"] == []
        assert payload["totals"]["impressions"] == 0


class TestLiveDateFilteredRows:
    """Date-range views query the Report API live — the cache has no time
    dimension to slice — and shape rows exactly like cache rows."""

    def _client(self, rows):
        captured = {}

        class FakeReporting:
            def preview(self, payload):
                captured["payload"] = payload
                return {"rows": rows}

        return SimpleNamespace(reporting=FakeReporting()), captured

    def test_builds_quick_range_request_and_stat_shaped_rows(self):
        from src.admin.blueprints.adapters import _improvedigital_live_stat_rows

        client, captured = self._client(
            [
                {
                    "campaign_id": 314446,
                    "line_item_id": 582321,
                    "impressions": "100",
                    "clicks": "3",
                    "advertiser_payout": "0.25",
                    "complete": "7",
                }
            ]
        )
        rows = _improvedigital_live_stat_rows(client, "t1", [314446, 314450], "LAST_7_DAYS", "UTC", "EUR")

        request = captured["payload"]["report_generation_request"]
        assert request["date_range"] == {"quick": "LAST_7_DAYS"}
        assert request["timezone"] == "UTC"
        assert request["currency_id"] == 1
        assert request["filters"] == [{"column": "campaign_id", "operation": "IN", "value": [314446, 314450]}]

        assert len(rows) == 1
        row = rows[0]
        assert (row.campaign_id, row.line_item_id) == ("314446", "582321")
        assert (row.impressions, row.clicks, row.completed_views) == (100, 3, 7)
        assert row.spend_micros == 250_000
        assert row.currency == "EUR"

    def test_today_translates_to_the_relative_range_the_wire_accepts(self):
        """quick TODAY 500s upstream (no same-day partition in the
        consolidated warehouse, observed live) — the boundary maps it to a
        1-day relative range that returns today's data."""
        from src.admin.blueprints.adapters import _improvedigital_live_stat_rows

        client, captured = self._client([])
        _improvedigital_live_stat_rows(client, "t1", [314446], "TODAY", "UTC", "EUR")
        assert captured["payload"]["report_generation_request"]["date_range"] == {
            "relative": {"from_count": 1, "from_unit": "DAY", "to_count": 0, "to_unit": "DAY"}
        }

    def test_no_campaigns_short_circuits_without_api_call(self):
        from src.admin.blueprints.adapters import _improvedigital_live_stat_rows

        client, captured = self._client([])
        assert _improvedigital_live_stat_rows(client, "t1", [], "TODAY", "UTC", "EUR") == []
        assert "payload" not in captured

    def test_live_rows_flow_through_the_shared_payload_builder(self):
        from src.admin.blueprints.adapters import _improvedigital_live_stat_rows

        client, _ = self._client(
            [
                {
                    "campaign_id": 314446,
                    "line_item_id": 582321,
                    "impressions": "100",
                    "clicks": "3",
                    "advertiser_payout": "0.25",
                    "complete": None,
                }
            ]
        )
        rows = _improvedigital_live_stat_rows(client, "t1", [314446], "TODAY", "UTC", "EUR")
        payload = _improvedigital_reporting_payload(rows, BUYS, "EUR")
        assert payload["rows"][0]["order_name"] == "Acme Summer Push"
        assert payload["totals"]["spend"] == 0.25
