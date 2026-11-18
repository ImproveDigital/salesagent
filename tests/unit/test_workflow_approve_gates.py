"""Workflows-page approve/reject must refuse what the media buy page refuses (FMS-2235).

The Workflows route marked a step approved before looking at the media buy, so:
- a buy with no creatives was booked on the ad server,
- a step already closed (e.g. by the buyer canceling) could be flipped to approved,
- a canceled/failed buy still accepted an approval.
"""

from types import SimpleNamespace

import pytest

from src.admin.blueprints.workflows import (
    _approve_blocked_error,
    _decided_step_error,
    _unapproved_creative_ids,
)


def _step(status="requires_approval", tool_name="create_media_buy"):
    return SimpleNamespace(step_id="step_1", status=status, tool_name=tool_name)


def _buy(status="pending_approval"):
    return SimpleNamespace(media_buy_id="mb_1", status=status)


def _assignment(creative_id="cr_1"):
    return SimpleNamespace(creative_id=creative_id, media_buy_id="mb_1")


class TestDecidedStep:
    @pytest.mark.parametrize("status", ["approved", "rejected", "completed", "failed"])
    @pytest.mark.parametrize("action", ["approve", "reject"])
    def test_decided_step_blocks_the_decision(self, status, action):
        error = _decided_step_error(_step(status=status), action)
        assert error is not None
        assert action in error
        assert status in error

    @pytest.mark.parametrize("status", ["requires_approval", "pending_approval"])
    def test_open_step_does_not_block(self, status):
        assert _decided_step_error(_step(status=status), "approve") is None


class TestApproveBlocked:
    @pytest.mark.parametrize("status", ["canceled", "completed", "rejected", "failed"])
    def test_closed_buy_blocks_approval(self, status):
        error = _approve_blocked_error(_step(), _buy(status=status), [_assignment()])
        assert error is not None
        assert status in error

    def test_pending_buy_without_creatives_blocks_approval(self):
        error = _approve_blocked_error(_step(), _buy(), [])
        assert error is not None
        assert "no creatives" in error

    def test_pending_buy_with_creatives_is_approvable(self):
        assert _approve_blocked_error(_step(), _buy(), [_assignment()]) is None

    def test_update_step_is_not_held_to_the_creative_rule(self):
        """An update step may be the very change that attaches the creatives."""
        assert _approve_blocked_error(_step(tool_name="update_media_buy"), _buy(), []) is None

    def test_step_without_media_buy_is_approvable(self):
        assert _approve_blocked_error(_step(tool_name="sync_creatives"), None, []) is None


class TestUnapprovedCreativeIds:
    def test_only_unapproved_creatives_are_reported(self):
        creatives = [
            SimpleNamespace(creative_id="cr_ok", status="approved"),
            SimpleNamespace(creative_id="cr_live", status="active"),
            SimpleNamespace(creative_id="cr_wait", status="pending_review"),
        ]
        assert _unapproved_creative_ids(creatives) == ["cr_wait"]
