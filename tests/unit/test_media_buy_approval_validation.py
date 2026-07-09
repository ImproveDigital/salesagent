"""Unit tests for media buy pre-approval validation gates.

Covers the required checks enforced before an admin can approve a media buy:
creatives must be assigned, none rejected, all approved, and the flight
window must be valid. See ``operations._validate_media_buy_approval``.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.admin.blueprints.operations import _validate_media_buy_approval

NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)


def _media_buy(start_offset_days=-1, end_offset_days=30):
    """Build a media buy stub with a flight window relative to NOW."""
    return SimpleNamespace(
        start_time=NOW + timedelta(days=start_offset_days),
        end_time=NOW + timedelta(days=end_offset_days),
    )


def _assignment(creative_id):
    return SimpleNamespace(creative_id=creative_id)


def _creative(creative_id, status):
    return SimpleNamespace(creative_id=creative_id, status=status)


def test_valid_media_buy_passes():
    assignments = [_assignment("cr_1"), _assignment("cr_2")]
    creatives = [_creative("cr_1", "approved"), _creative("cr_2", "active")]

    assert _validate_media_buy_approval(_media_buy(), assignments, creatives, NOW) is None


def test_no_creatives_assigned_blocks():
    error = _validate_media_buy_approval(_media_buy(), [], [], NOW)

    assert error is not None
    assert "no creatives" in error.lower()


def test_rejected_creative_blocks():
    assignments = [_assignment("cr_1"), _assignment("cr_2")]
    creatives = [_creative("cr_1", "approved"), _creative("cr_2", "rejected")]

    error = _validate_media_buy_approval(_media_buy(), assignments, creatives, NOW)

    assert error is not None
    assert "rejected" in error.lower()
    assert "cr_2" in error


def test_pending_creative_blocks():
    assignments = [_assignment("cr_1"), _assignment("cr_2")]
    creatives = [_creative("cr_1", "approved"), _creative("cr_2", "pending_review")]

    error = _validate_media_buy_approval(_media_buy(), assignments, creatives, NOW)

    assert error is not None
    assert "not yet approved" in error.lower()
    assert "cr_2" in error


def test_missing_creative_row_treated_as_unapproved():
    # Assignment references a creative that no longer exists in the creatives table.
    assignments = [_assignment("cr_1"), _assignment("cr_missing")]
    creatives = [_creative("cr_1", "approved")]

    error = _validate_media_buy_approval(_media_buy(), assignments, creatives, NOW)

    assert error is not None
    assert "cr_missing" in error


def test_rejected_takes_priority_over_pending():
    assignments = [_assignment("cr_pending"), _assignment("cr_rejected")]
    creatives = [
        _creative("cr_pending", "pending_review"),
        _creative("cr_rejected", "rejected"),
    ]

    error = _validate_media_buy_approval(_media_buy(), assignments, creatives, NOW)

    assert error is not None
    assert "rejected" in error.lower()


def test_ended_flight_window_blocks():
    assignments = [_assignment("cr_1")]
    creatives = [_creative("cr_1", "approved")]
    media_buy = _media_buy(start_offset_days=-30, end_offset_days=-1)

    error = _validate_media_buy_approval(media_buy, assignments, creatives, NOW)

    assert error is not None
    assert "ended" in error.lower()


def test_end_before_start_blocks():
    assignments = [_assignment("cr_1")]
    creatives = [_creative("cr_1", "approved")]
    media_buy = _media_buy(start_offset_days=10, end_offset_days=5)

    error = _validate_media_buy_approval(media_buy, assignments, creatives, NOW)

    assert error is not None
    assert "after the start" in error.lower()


def test_naive_datetimes_are_treated_as_utc():
    assignments = [_assignment("cr_1")]
    creatives = [_creative("cr_1", "approved")]
    media_buy = SimpleNamespace(
        start_time=datetime(2026, 6, 1, 12, 0, 0),  # naive
        end_time=datetime(2026, 7, 1, 12, 0, 0),  # naive, future
    )

    assert _validate_media_buy_approval(media_buy, assignments, creatives, NOW) is None


def test_missing_flight_window_is_allowed():
    assignments = [_assignment("cr_1")]
    creatives = [_creative("cr_1", "approved")]
    media_buy = SimpleNamespace(start_time=None, end_time=None)

    assert _validate_media_buy_approval(media_buy, assignments, creatives, NOW) is None


@pytest.mark.parametrize("status", ["approved", "active"])
def test_ready_statuses_pass(status):
    assignments = [_assignment("cr_1")]
    creatives = [_creative("cr_1", status)]

    assert _validate_media_buy_approval(_media_buy(), assignments, creatives, NOW) is None
