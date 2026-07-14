"""Unit tests for the GAM order status watcher lifecycle.

Covers the DB-backed pieces of ``order_approval_service``:
- watch deadline resolves to the media buy's end datetime (fallback 24 h)
- the poll interval default comes from GAM_ORDER_STATUS_POLL_INTERVAL_SECONDS
- watchers left 'running' by a dead process are resumed on startup
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import src.services.order_approval_service as service
from src.services.order_approval_service import (
    _STATUS_POLL_FALLBACK_WINDOW,
    _watch_deadline,
    resume_order_status_watchers,
    start_order_status_polling,
)

STARTED_AT = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def _mock_session_returning(first_result=None, all_results=None):
    """Context-manager mock for get_db_session()."""
    db = MagicMock()
    db.scalars.return_value.first.return_value = first_result
    db.scalars.return_value.all.return_value = all_results or []
    ctx = MagicMock()
    ctx.__enter__.return_value = db
    ctx.__exit__.return_value = False
    return ctx, db


class TestWatchDeadline:
    def test_uses_media_buy_end_time(self):
        end = STARTED_AT + timedelta(days=14)
        buy = MagicMock(end_time=end, end_date=end.date())
        ctx, _ = _mock_session_returning(first_result=buy)
        with patch.object(service, "get_db_session", return_value=ctx):
            assert _watch_deadline("mb_1", "order_1", "tenant_1", STARTED_AT) == end

    def test_uses_end_date_when_no_end_time(self):
        end_date = (STARTED_AT + timedelta(days=7)).date()
        buy = MagicMock(end_time=None, end_date=end_date)
        ctx, _ = _mock_session_returning(first_result=buy)
        with patch.object(service, "get_db_session", return_value=ctx):
            deadline = _watch_deadline("mb_1", "order_1", "tenant_1", STARTED_AT)
        assert deadline.date() == end_date
        assert deadline.hour == 23  # inclusive: watch through end of day

    def test_falls_back_to_24h_when_buy_missing(self):
        ctx, _ = _mock_session_returning(first_result=None)
        with patch.object(service, "get_db_session", return_value=ctx):
            deadline = _watch_deadline("mb_1", "order_1", "tenant_1", STARTED_AT)
        assert deadline == STARTED_AT + _STATUS_POLL_FALLBACK_WINDOW


class TestStartOrderStatusPolling:
    def test_default_interval_comes_from_module_env_constant(self):
        ctx, db = _mock_session_returning(all_results=[])
        with (
            patch.object(service, "get_db_session", return_value=ctx),
            patch.object(service, "_spawn_status_watch_thread") as spawn,
            patch.object(service, "_STATUS_POLL_INTERVAL_SECONDS", 77),
        ):
            start_order_status_polling(
                order_id="order_1",
                media_buy_id="mb_1",
                tenant_id="tenant_1",
                principal_id="principal_1",
            )
        assert spawn.call_args.kwargs["poll_interval_seconds"] == 77
        job = db.add.call_args.args[0]
        assert job.progress["poll_interval_seconds"] == 77


class TestResumeOrderStatusWatchers:
    def _job(self, sync_id="watch_1", **progress_overrides):
        progress = {
            "order_id": "order_1",
            "media_buy_id": "mb_1",
            "principal_id": "principal_1",
            "webhook_url": None,
            "poll_interval_seconds": 30,
        }
        progress.update(progress_overrides)
        job = MagicMock()
        job.sync_id = sync_id
        job.tenant_id = "tenant_1"
        job.progress = progress
        return job

    def test_resumes_running_watch_jobs(self):
        ctx, _ = _mock_session_returning(all_results=[self._job()])
        with (
            patch.object(service, "get_db_session", return_value=ctx),
            patch.object(service, "_spawn_status_watch_thread") as spawn,
        ):
            assert resume_order_status_watchers() == 1
        kwargs = spawn.call_args.kwargs
        assert kwargs["approval_id"] == "watch_1"
        assert kwargs["order_id"] == "order_1"
        assert kwargs["media_buy_id"] == "mb_1"

    def test_skips_watchers_already_running_in_process(self):
        ctx, _ = _mock_session_returning(all_results=[self._job()])
        with (
            patch.object(service, "get_db_session", return_value=ctx),
            patch.object(service, "_spawn_status_watch_thread") as spawn,
            patch.dict(service._active_approvals, {"watch_1": MagicMock()}),
        ):
            assert resume_order_status_watchers() == 0
        spawn.assert_not_called()

    def test_skips_jobs_missing_order_context(self):
        ctx, _ = _mock_session_returning(all_results=[self._job(order_id=None)])
        with (
            patch.object(service, "get_db_session", return_value=ctx),
            patch.object(service, "_spawn_status_watch_thread") as spawn,
        ):
            assert resume_order_status_watchers() == 0
        spawn.assert_not_called()
