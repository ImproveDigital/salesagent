"""Unit tests for the internal-only ``pending_ad_server_approval`` media buy status.

Covers the pure status logic:
- Admin UI readiness never derives live/scheduled while the GAM order is unapproved
- the status is internal-only: coerced off the AdCP wire like ``pending_approval``
- post-approval transition picks scheduled/active/completed from the flight window
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.admin.services.media_buy_readiness_service import MediaBuyReadinessService
from src.core.database.models import MediaBuy
from src.core.tools.media_buy_create import _media_buy_status_for_create_replay
from src.core.tools.media_buy_list import _compute_status, _to_wire_status
from src.services.order_approval_service import _post_approval_status

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def _media_buy(status: str, start_offset_days: int, end_offset_days: int) -> MediaBuy:
    start = NOW + timedelta(days=start_offset_days)
    end = NOW + timedelta(days=end_offset_days)
    return MediaBuy(
        media_buy_id="mb_test",
        tenant_id="tenant_1",
        principal_id="principal_1",
        order_name="Order",
        advertiser_name="Advertiser",
        status=status,
        start_date=start.date(),
        end_date=end.date(),
        start_time=start,
        end_time=end,
        raw_request={},
    )


def _readiness_state(buy: MediaBuy) -> str:
    return MediaBuyReadinessService._compute_state(
        media_buy=buy,
        now=NOW,
        packages_total=1,
        packages_with_creatives=1,
        creatives_total=1,
        creatives_approved=1,
        creatives_pending=0,
        creatives_rejected=0,
        blocking_issues=[],
    )


class TestReadinessShortCircuit:
    def test_mid_flight_pending_ad_server_approval_is_not_live(self):
        buy = _media_buy("pending_ad_server_approval", start_offset_days=-2, end_offset_days=5)
        assert _readiness_state(buy) == "pending_ad_server_approval"

    def test_future_flight_pending_ad_server_approval_is_not_scheduled(self):
        buy = _media_buy("pending_ad_server_approval", start_offset_days=3, end_offset_days=10)
        assert _readiness_state(buy) == "pending_ad_server_approval"

    def test_active_buy_mid_flight_still_derives_live(self):
        buy = _media_buy("active", start_offset_days=-2, end_offset_days=5)
        assert _readiness_state(buy) == "live"


class TestWireMapping:
    """External mapping mirrors ``pending_approval``: never on the AdCP wire."""

    def test_to_wire_status_drops_pending_ad_server_approval(self):
        assert _to_wire_status("pending_ad_server_approval") is None

    def test_create_replay_maps_to_pending_start(self):
        existing = SimpleNamespace(
            status="pending_ad_server_approval",
            raw_request={"packages": [{"product_id": "p1", "creative_ids": ["c1"]}]},
        )
        assert _media_buy_status_for_create_replay(existing).value == "pending_start"

    def test_get_media_buys_maps_to_pending_start(self):
        # Same external behavior as pending_approval: nothing is delivering
        # while the approval blocker is persisted, so listing reports
        # pending_start even mid-flight — never date-derived active.
        buy = _media_buy("pending_ad_server_approval", start_offset_days=3, end_offset_days=10)
        assert _compute_status(buy, NOW.date()).value == "pending_start"

    def test_get_media_buys_mid_flight_is_not_active(self):
        buy = _media_buy("pending_ad_server_approval", start_offset_days=-2, end_offset_days=5)
        assert _compute_status(buy, NOW.date()).value == "pending_start"


class TestPostApprovalStatus:
    def test_future_start_becomes_scheduled(self):
        buy = _media_buy("pending_ad_server_approval", start_offset_days=3, end_offset_days=10)
        assert _post_approval_status(buy, now=NOW) == "scheduled"

    def test_mid_flight_becomes_active(self):
        buy = _media_buy("pending_ad_server_approval", start_offset_days=-1, end_offset_days=5)
        assert _post_approval_status(buy, now=NOW) == "active"

    def test_past_end_becomes_completed(self):
        buy = _media_buy("pending_ad_server_approval", start_offset_days=-10, end_offset_days=-1)
        assert _post_approval_status(buy, now=NOW) == "completed"

    def test_date_only_buy_uses_date_columns(self):
        buy = _media_buy("pending_ad_server_approval", start_offset_days=2, end_offset_days=9)
        buy.start_time = None
        buy.end_time = None
        assert _post_approval_status(buy, now=NOW) == "scheduled"
