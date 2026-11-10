"""FMS-2152: Media Buys list filters.

- "Needs Creatives" includes buys with creative gaps even when a higher-priority
  state (e.g. awaiting approval) drives the badge.
- "Ready" maps to the computed "scheduled" state (no "ready" state exists).
- "Pending ad server" chip exists; unknown ?status values fall back to All.
"""

from datetime import date
from types import SimpleNamespace

import pytest
from flask import render_template

from src.admin.app import create_app
from src.admin.services.media_buy_readiness_service import (
    LIST_FILTERS,
    matches_list_filter,
    normalize_list_filter,
)


def _readiness(state, packages_total=1, packages_with_creatives=1, creatives_total=1, pending=0, rejected=0):
    return {
        "state": state,
        "packages_total": packages_total,
        "packages_with_creatives": packages_with_creatives,
        "creatives_total": creatives_total,
        "creatives_pending": pending,
        "creatives_rejected": rejected,
    }


class TestNormalizeListFilter:
    @pytest.mark.parametrize("value", [None, "", "bogus", "draft"])
    def test_unknown_values_fall_back_to_all(self, value):
        assert normalize_list_filter(value) is None

    def test_legacy_ready_maps_to_scheduled(self):
        assert normalize_list_filter("ready") == "scheduled"

    def test_pending_ad_server_is_a_filter(self):
        assert normalize_list_filter("pending_ad_server_approval") == "pending_ad_server_approval"


class TestMatchesListFilter:
    def test_pending_approval_buy_without_creatives_shows_under_needs_creatives(self):
        """mb_4f866d3b9846: needs_approval badge, 0/1 packages ready, no creatives."""
        readiness = _readiness("needs_approval", packages_with_creatives=0, creatives_total=0)
        assert matches_list_filter(readiness, "needs_creatives")
        assert matches_list_filter(readiness, "needs_approval")

    def test_pending_ad_server_buy_with_rejected_creative_needs_creatives(self):
        readiness = _readiness("pending_ad_server_approval", rejected=1)
        assert matches_list_filter(readiness, "needs_creatives")
        assert matches_list_filter(readiness, "pending_ad_server_approval")

    def test_fully_creatived_approval_buy_not_under_needs_creatives(self):
        assert not matches_list_filter(_readiness("needs_approval"), "needs_creatives")

    @pytest.mark.parametrize("state", ["completed", "failed"])
    def test_finished_buys_never_need_creatives(self, state):
        readiness = _readiness(state, packages_with_creatives=0, creatives_total=0)
        assert not matches_list_filter(readiness, "needs_creatives")

    def test_other_filters_match_state_exactly(self):
        assert matches_list_filter(_readiness("scheduled"), "scheduled")
        assert not matches_list_filter(_readiness("live"), "scheduled")


@pytest.fixture
def app():
    return create_app({"TESTING": True, "SECRET_KEY": "test", "WTF_CSRF_ENABLED": False})


def _row():
    mb = SimpleNamespace(
        media_buy_id="mb_4f866d3b9846",
        order_name="Apostra Q4",
        budget=1000,
        currency="EUR",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 30),
        status="pending_ad_server_approval",
    )
    return {
        "media_buy": mb,
        "readiness_state": "pending_ad_server_approval",
        "is_ready": False,
        "principal_name": "Apostra",
        "product_names": [],
        "packages_ready": 0,
        "packages_total": 1,
        "blocking_issues": [],
    }


class TestMediaBuysListTemplate:
    def _render(self, app, status_filter=None):
        counts = dict.fromkeys(LIST_FILTERS, 0)
        counts["pending_ad_server_approval"] = 3
        with app.test_request_context():
            return render_template(
                "media_buys_list.html",
                tenant=SimpleNamespace(name="Azerion NL", tenant_id="t1"),
                tenant_id="t1",
                media_buys=[_row()],
                status_filter=status_filter,
                list_filters=LIST_FILTERS,
                filter_counts=counts,
                total_count=5,
            )

    def test_every_filter_has_a_chip_with_its_count(self, app):
        html = self._render(app)
        for key, label in LIST_FILTERS.items():
            assert f"status={key}" in html
            assert label in html
        assert "Pending ad server" in html
        assert ">3</span>" in html

    def test_no_separate_id_column(self, app):
        html = self._render(app)
        assert "<th>ID</th>" not in html
        assert "mb_4f866d3b9846" in html  # still visible under the name

    def test_long_statuses_render_wrappable(self, app):
        assert "pending ad server approval" in self._render(app)
