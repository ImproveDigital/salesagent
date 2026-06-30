"""Tests for submission-time validation on update_media_buy.

These cover the fix that moves the highest-risk input checks ahead of the
manual-approval gate (#M1) and rejects a concrete past start_time (#B2).
Previously these checks only ran on the apply path, which sits *after* the
manual-approval deferral — so a bad update (negative budget, end<=start,
past start) was accepted as ``requires_approval`` and only failed later on
approval replay.

``_validate_update_submission`` is a pure function: it takes the request,
the current media buy, and ``now``, and returns ``UpdateMediaBuyError`` or
``None``. No DB or transport involved.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from src.core.schemas import UpdateMediaBuyError, UpdateMediaBuyRequest
from src.core.tools.media_buy_update import _validate_update_submission
from tests.factories.spec_required_kwargs import required_request_kwargs

NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)


def _buy(start=datetime(2026, 6, 1, tzinfo=UTC), end=datetime(2026, 7, 31, tzinfo=UTC)):
    b = MagicMock()
    b.start_time = start
    b.end_time = end
    return b


def _req(**kwargs):
    return UpdateMediaBuyRequest(
        **required_request_kwargs(idempotency_key="subval-key-123456"), media_buy_id="mb_1", **kwargs
    )


class TestBudgetValidation:
    def test_negative_budget_rejected(self):
        result = _validate_update_submission(_req(ext={"salesagent": {"budget": -5}}), _buy(), NOW)
        assert isinstance(result, UpdateMediaBuyError)
        assert result.errors[0].code == "invalid_budget"

    def test_zero_budget_rejected(self):
        result = _validate_update_submission(_req(ext={"salesagent": {"budget": 0}}), _buy(), NOW)
        assert isinstance(result, UpdateMediaBuyError)
        assert result.errors[0].code == "invalid_budget"

    def test_positive_budget_allowed(self):
        assert _validate_update_submission(_req(ext={"salesagent": {"budget": 2}}), _buy(), NOW) is None


class TestDateRangeValidation:
    def test_start_after_end_rejected(self):
        result = _validate_update_submission(
            _req(start_time="2026-08-01T00:00:00Z", end_time="2026-07-01T00:00:00Z"), _buy(), NOW
        )
        assert isinstance(result, UpdateMediaBuyError)
        assert result.errors[0].code == "invalid_date_range"

    def test_start_equals_end_rejected(self):
        result = _validate_update_submission(
            _req(start_time="2026-08-05T00:00:00Z", end_time="2026-08-05T00:00:00Z"), _buy(), NOW
        )
        assert isinstance(result, UpdateMediaBuyError)
        assert result.errors[0].code == "invalid_date_range"

    def test_new_end_before_existing_start_rejected(self):
        # Only end_time supplied; falls back to the buy's persisted start.
        result = _validate_update_submission(_req(end_time="2020-01-01T00:00:00Z"), _buy(), NOW)
        assert isinstance(result, UpdateMediaBuyError)
        assert result.errors[0].code == "invalid_date_range"

    def test_valid_future_window_allowed(self):
        assert (
            _validate_update_submission(
                _req(start_time="2026-08-01T00:00:00Z", end_time="2026-09-01T00:00:00Z"), _buy(), NOW
            )
            is None
        )

    def test_no_date_change_skips_range_check_even_if_existing_bounds_inverted(self):
        # A pure budget update must not be rejected for the buy's pre-existing
        # date bounds — the range check only applies when a date is changing.
        inverted = _buy(start=datetime(2026, 8, 1, tzinfo=UTC), end=datetime(2026, 7, 1, tzinfo=UTC))
        assert _validate_update_submission(_req(ext={"salesagent": {"budget": 3}}), inverted, NOW) is None


class TestPastStartValidation:
    def test_concrete_past_start_rejected(self):
        result = _validate_update_submission(_req(start_time="2020-01-01T00:00:00Z"), _buy(), NOW)
        assert isinstance(result, UpdateMediaBuyError)
        assert result.errors[0].code == "invalid_start_time"

    def test_asap_always_allowed(self):
        # 'asap' resolves to now and must never trip the past-start guard.
        assert _validate_update_submission(_req(start_time="asap"), _buy(), NOW) is None

    def test_future_start_allowed(self):
        # Future start within the buy's existing flight window (ends 2026-07-31).
        assert _validate_update_submission(_req(start_time="2026-07-15T00:00:00Z"), _buy(), NOW) is None

    def test_past_start_allowed_on_replay(self):
        # On approval replay (allow_past_start=True) a start that was valid at
        # submission but has since become past must still apply.
        assert (
            _validate_update_submission(_req(start_time="2020-01-01T00:00:00Z"), _buy(), NOW, allow_past_start=True)
            is None
        )

    def test_unchanged_past_start_not_checked(self):
        # No start_time supplied → the buy's existing (past) start is untouched
        # and must not be re-validated.
        assert _validate_update_submission(_req(ext={"salesagent": {"budget": 2}}), _buy(), NOW) is None
