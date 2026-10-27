"""The media buy detail page never waits on the ad server.

The page renders from the database snapshot, then fetches the delivery
refresh once. The endpoint pulls from the ad server, writes the snapshot
and returns the rendered metrics partial; a failure degrades the page to
its snapshot with a retry link instead of a 500.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

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


def _fetch(buy, principal):
    app = create_app()
    with (
        app.test_request_context("/tenant/t1/media-buy/mb_1/delivery-metrics"),
        patch("src.core.database.database_session.get_db_session"),
        patch(f"{_MOD}.MediaBuyRepository") as buy_repo_cls,
        patch("src.core.database.repositories.principal.PrincipalRepository") as principal_repo_cls,
    ):
        buy_repo_cls.return_value.get_by_id.return_value = buy
        principal_repo_cls.return_value.get_by_id.return_value = principal
        return operations._delivery_metrics_payload("t1", "mb_1")


def test_fetch_returns_the_rendered_partial_and_a_summary():
    metrics = {
        "impressions": 1002,
        "spend": 0.62,
        "currency": "EUR",
        "clicks": 3,
        "ctr": 0.3,
        "by_package": [SimpleNamespace(package_id="202", impressions=1002, spend=0.62, clicks=3)],
    }
    with patch(f"{_MOD}._sync_delivery_metrics", return_value=metrics) as sync:
        payload, status = _fetch(_buy(), SimpleNamespace(principal_id="p_1"))

    assert status == 200
    sync.assert_called_once()
    assert payload["delivery_metrics"] == {"impressions": 1002, "spend": 0.62, "currency": "EUR", "clicks": 3}
    assert "EUR 0.62" in payload["html"]
    assert "Line Item" in payload["html"] and "202" in payload["html"]


def test_nothing_delivered_renders_the_snapshot_note():
    with patch(f"{_MOD}._sync_delivery_metrics", return_value=None):
        payload, status = _fetch(_buy(status="draft"), SimpleNamespace(principal_id="p_1"))

    assert status == 200
    assert payload["delivery_metrics"] is None
    assert "No delivery reported" in payload["html"]


def test_missing_buy_is_404():
    _, status = _fetch(None, None)

    assert status == 404


def test_ad_server_failure_answers_502_json_not_a_crash():
    app = create_app()
    with (
        app.test_request_context("/tenant/t1/media-buy/mb_1/delivery-metrics"),
        patch(f"{_MOD}._delivery_metrics_payload", side_effect=RuntimeError("GAM report job timed out")),
    ):
        response = operations.media_buy_delivery_metrics.__wrapped__("t1", "mb_1")

    assert response[1] == 502
    assert "Could not fetch delivery metrics" in response[0].get_json()["error"]


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
