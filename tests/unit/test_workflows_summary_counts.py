"""Workflows page dashboard cards: Pending Tasks and Completed Today.

Regression: Pending Tasks counted only ``pending_approval`` steps, but approval
steps are created as ``requires_approval``, so the card read 0 while tasks were
waiting. Completed Today was hardcoded to 0.
"""

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

from src.admin.blueprints.workflows import _task_summary_counts

NOW = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)


def _step(status: str, completed_at: datetime | None = None) -> SimpleNamespace:
    return SimpleNamespace(status=status, completed_at=completed_at)


def test_pending_tasks_counts_requires_approval_and_pending_approval():
    steps = [
        _step("requires_approval"),
        _step("requires_approval"),
        _step("pending_approval"),
        _step("in_progress"),
        _step("completed", NOW),
    ]

    assert _task_summary_counts(steps, NOW)["pending_tasks"] == 3


def test_completed_today_counts_steps_finished_or_decided_today():
    steps = [
        _step("completed", NOW - timedelta(hours=1)),
        _step("approved", NOW - timedelta(hours=2)),
        _step("rejected", NOW - timedelta(hours=3)),
    ]

    assert _task_summary_counts(steps, NOW)["completed_today"] == 3


def test_completed_today_ignores_other_days_failures_and_open_steps():
    steps = [
        _step("completed", NOW - timedelta(days=1)),
        _step("failed", NOW - timedelta(hours=1)),
        _step("requires_approval"),
        _step("approved", None),
    ]

    assert _task_summary_counts(steps, NOW)["completed_today"] == 0


def test_completed_today_uses_the_utc_day():
    # 01:30 at UTC+2 on the 19th is 23:30 UTC on the 18th.
    late_utc = datetime(2026, 11, 19, 1, 30, tzinfo=timezone(timedelta(hours=2)))
    naive_utc = datetime(2026, 11, 18, 9, 0)
    steps = [_step("completed", late_utc), _step("completed", naive_utc)]

    assert _task_summary_counts(steps, NOW)["completed_today"] == 2
