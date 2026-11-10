"""FMS-2148: media buys whose flight ended while awaiting approval.

- The status scheduler expires approval-pending buys past their flight end.
- The approval gate reports an ended flight before any creative issue.
- The dashboard excludes ended flights from Incoming and "expiring in <24h".
"""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.admin.app import create_app
from src.admin.blueprints.operations import _validate_media_buy_approval
from src.admin.services.dashboard_service import DashboardService
from src.core.helpers.media_buy_flight import flight_end_utc, flight_has_ended
from src.services.media_buy_status_scheduler import MediaBuyStatusScheduler

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


def _buy(status="pending_approval", start_offset=timedelta(days=-6), end_offset=timedelta(days=-4), **extra):
    fields = {
        "status": status,
        "start_time": NOW + start_offset,
        "end_time": NOW + end_offset,
        "start_date": None,
        "end_date": None,
        "media_buy_id": "mb_test",
        "tenant_id": "t1",
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


class TestFlightHelpers:
    def test_end_time_naive_is_treated_as_utc(self):
        buy = SimpleNamespace(end_time=datetime(2026, 11, 1, 23, 59, 59), end_date=None)
        assert flight_end_utc(buy) == datetime(2026, 11, 1, 23, 59, 59, tzinfo=UTC)

    def test_end_date_fallback_is_inclusive_end_of_day(self):
        buy = SimpleNamespace(end_time=None, end_date=date(2026, 11, 5))
        assert not flight_has_ended(buy, NOW)
        assert flight_has_ended(buy, datetime(2026, 11, 6, 0, 0, 0, tzinfo=UTC))

    def test_no_flight_end_never_ended(self):
        assert not flight_has_ended(SimpleNamespace(end_time=None, end_date=None), NOW)


class TestSchedulerExpiresApprovalPendingBuys:
    @pytest.mark.parametrize("status", ["pending_approval", "pending_ad_server_approval"])
    def test_ended_flight_becomes_canceled(self, status):
        new_status = MediaBuyStatusScheduler()._compute_new_status(_buy(status), NOW, session=None)
        assert new_status == "canceled"

    @pytest.mark.parametrize("status", ["pending_approval", "pending_ad_server_approval"])
    def test_in_flight_buy_stays_pending(self, status):
        """Approval-pending buys are never auto-activated by date math."""
        buy = _buy(status, start_offset=timedelta(days=-1), end_offset=timedelta(days=3))
        assert MediaBuyStatusScheduler()._compute_new_status(buy, NOW, session=None) is None

    def test_active_buy_still_completes(self):
        assert MediaBuyStatusScheduler()._compute_new_status(_buy("active"), NOW, session=None) == "completed"


class TestApprovalGateFlightEndedFirst:
    def test_ended_flight_reported_before_missing_creatives(self):
        error = _validate_media_buy_approval(_buy(), [], [], NOW)
        assert error == "Cannot approve: the flight window has already ended."


@pytest.fixture
def _request_context():
    app = create_app({"TESTING": True, "SECRET_KEY": "test", "WTF_CSRF_ENABLED": False})
    with app.test_request_context():
        yield


def _dashboard_buy(media_buy_id, end_time, created_hours_ago=48):
    return SimpleNamespace(
        media_buy_id=media_buy_id,
        advertiser_name=media_buy_id,
        order_name="",
        budget=1,
        currency="EUR",
        created_at=datetime.now(UTC) - timedelta(hours=created_hours_ago),
        end_time=end_time,
        end_date=end_time.date(),
        is_paused=False,
    )


class TestDashboardExcludesEndedFlights:
    def test_incoming_skips_ended_offers(self):
        now = datetime.now(UTC)
        ended = _dashboard_buy("mb_ended", now - timedelta(days=4))
        open_offer = _dashboard_buy("mb_open", now + timedelta(days=4))
        repo = Mock()
        repo.list_by_statuses.return_value = [ended, open_offer]

        incoming = DashboardService("test_tenant")._incoming(repo)

        assert incoming["count"] == 1
        assert [r["media_buy_id"] for r in incoming["rows"]] == ["mb_open"]
        assert incoming["urgent_count"] == 1

    @pytest.mark.usefixtures("_request_context")
    def test_expiring_excludes_ended_and_beyond_24h(self):
        now = datetime.now(UTC)
        ended = _dashboard_buy("mb_ended", now - timedelta(minutes=5))
        later = _dashboard_buy("mb_later", now + timedelta(hours=30))
        soon = _dashboard_buy("mb_soon", now + timedelta(hours=3))
        session = Mock()
        session.scalar.return_value = 0
        repo = Mock()
        repo.list_by_statuses.return_value = [ended, later, soon]
        service = DashboardService("test_tenant")
        service._running_row = lambda _b, _n: {"pacing": "ok", "delivery_pct": 0.5, "flight_pct": 0.5}

        items = service._needs_attention(session, repo)

        expiring = [i for i in items if "expiring" in i["title"]]
        assert len(expiring) == 1
        assert expiring[0]["title"] == "1 deal expiring in <24h"
        assert expiring[0]["url"] == "/tenant/test_tenant/media-buy/mb_soon"
