"""The media buy detail page never waits on the ad server.

The page renders from the database. A poll endpoint starts the delivery
refresh on a background thread and answers ``pending`` until the thread has
fetched from the ad server and written the snapshot; then it returns the
rendered metrics partial. Every HTTP request is short, so a proxy timeout
in front of the app (dev saw gateway timeouts) can no longer trigger.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.admin.app import create_app
from src.admin.blueprints import operations

_MOD = "src.admin.blueprints.operations"


def _buy(status: str = "active", **extra) -> SimpleNamespace:
    fields = {
        "media_buy_id": "mb_1",
        "principal_id": "p_1",
        "status": status,
        "adapter_type": "improvedigital",
        "raw_request": {},
        "delivered_amount": None,
        "delivered_impressions": None,
        "delivery_synced_at": None,
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


def _poll(buy, principal, *, thread_runs: bool):
    """One poll. ``thread_runs`` makes the refresh thread run inline instead of in the background."""
    app = create_app()
    with (
        app.test_request_context("/tenant/t1/media-buy/mb_1/delivery-metrics"),
        patch("src.core.database.database_session.get_db_session"),
        patch(f"{_MOD}.MediaBuyRepository") as buy_repo_cls,
        patch("src.core.database.repositories.principal.PrincipalRepository") as principal_repo_cls,
        patch(f"{_MOD}._delivery_refresh_executor") as executor,
    ):
        buy_repo_cls.return_value.get_by_id.return_value = buy
        principal_repo_cls.return_value.get_by_id.return_value = principal
        if thread_runs:
            executor.submit.side_effect = lambda fn, *args: fn(*args)
        else:
            executor.submit.return_value = MagicMock()
        payload, status = operations._delivery_metrics_payload("t1", "mb_1")
    return payload, status, executor


def setup_function():
    operations._delivery_refreshes.clear()


def test_first_poll_starts_the_refresh_and_answers_pending_immediately():
    with patch(f"{_MOD}._sync_delivery_metrics") as sync:
        payload, status, thread_cls = _poll(_buy(), SimpleNamespace(principal_id="p_1"), thread_runs=False)

    assert (status, payload) == (200, {"state": "pending"})
    sync.assert_not_called()  # the fetch happens on the executor, not in the request
    thread_cls.submit.assert_called_once_with(operations._run_delivery_refresh, "t1", "mb_1")
    assert ("t1", "mb_1") in operations._delivery_refreshes


def test_poll_after_the_refresh_finished_returns_the_rendered_partial():
    metrics = {
        "impressions": 1002,
        "spend": 0.62,
        "currency": "EUR",
        "clicks": 3,
        "ctr": 0.3,
        "by_package": [SimpleNamespace(package_id="202", impressions=1002, spend=0.62, clicks=3)],
    }
    with patch(f"{_MOD}._sync_delivery_metrics", return_value=metrics) as sync:
        payload, status, _ = _poll(_buy(), SimpleNamespace(principal_id="p_1"), thread_runs=True)

    assert status == 200
    sync.assert_called_once()
    assert payload["state"] == "done"
    assert payload["delivery_metrics"] == {"impressions": 1002, "spend": 0.62, "currency": "EUR", "clicks": 3}
    assert "EUR 0.62" in payload["html"]
    assert "Line Item" in payload["html"] and "202" in payload["html"]
    assert ("t1", "mb_1") not in operations._delivery_refreshes  # collected; next view refreshes again


def test_refresh_error_is_reported_not_raised():
    with patch(f"{_MOD}._sync_delivery_metrics", side_effect=RuntimeError("GAM report job timed out")):
        payload, status, _ = _poll(_buy(), SimpleNamespace(principal_id="p_1"), thread_runs=True)

    assert status == 200
    assert payload["state"] == "error"
    assert "timed out" in payload["error"]


def test_second_poll_while_running_does_not_start_another_thread():
    with patch(f"{_MOD}._sync_delivery_metrics"):
        _poll(_buy(), SimpleNamespace(principal_id="p_1"), thread_runs=False)
        payload, _, thread_cls = _poll(_buy(), SimpleNamespace(principal_id="p_1"), thread_runs=False)

    assert payload == {"state": "pending"}
    thread_cls.submit.assert_not_called()


def test_ineligible_buy_is_done_without_the_ad_server():
    with patch(f"{_MOD}._sync_delivery_metrics") as sync:
        payload, status, _ = _poll(_buy(status="draft"), SimpleNamespace(principal_id="p_1"), thread_runs=True)

    assert status == 200
    sync.assert_not_called()
    assert payload["state"] == "done"
    assert "No delivery reported" in payload["html"]


def test_missing_buy_is_404():
    _, status, _ = _poll(None, None, thread_runs=True)

    assert status == 404


def test_partial_renders_snapshot_note_when_nothing_delivered():
    app = create_app()
    from flask import render_template

    with app.test_request_context("/"):
        html = render_template(
            "_media_buy_delivery_metrics.html",
            delivery_metrics=None,
            media_buy=_buy(delivery_synced_at=datetime(2026, 10, 22, 15, 0, tzinfo=UTC)),
        )
    assert "No delivery reported" in html
    assert "2026-10-22 15:00 UTC" in html
