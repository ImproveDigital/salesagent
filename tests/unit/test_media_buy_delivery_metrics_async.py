"""The media buy detail page no longer blocks on the ad server.

Delivery metrics are fetched by a separate request after the page renders:
``operations.media_buy_delivery_metrics`` runs the same snapshot-writing
sync and returns the rendered metrics partial, which the page swaps in.
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


def _payload(buy, principal, metrics):
    app = create_app()
    with (
        app.test_request_context("/tenant/t1/media-buy/mb_1/delivery-metrics"),
        patch("src.core.database.database_session.get_db_session"),
        patch(f"{_MOD}.MediaBuyRepository") as buy_repo_cls,
        patch("src.core.database.repositories.principal.PrincipalRepository") as principal_repo_cls,
        patch(f"{_MOD}._sync_delivery_metrics", return_value=metrics) as sync,
    ):
        buy_repo_cls.return_value.get_by_id.return_value = buy
        principal_repo_cls.return_value.get_by_id.return_value = principal
        payload, status = operations._delivery_metrics_payload("t1", "mb_1")
    return payload, status, sync


def test_endpoint_runs_the_sync_and_returns_the_rendered_partial():
    metrics = {
        "impressions": 1002,
        "spend": 0.62,
        "currency": "EUR",
        "clicks": 3,
        "ctr": 0.3,
        "by_package": [SimpleNamespace(package_id="202", impressions=1002, spend=0.62, clicks=3)],
    }

    payload, status, sync = _payload(_buy(), SimpleNamespace(principal_id="p_1"), metrics)

    assert status == 200
    sync.assert_called_once()
    assert payload["delivery_metrics"] == {"impressions": 1002, "spend": 0.62, "currency": "EUR", "clicks": 3}
    assert "EUR 0.62" in payload["html"]
    assert "Line Item" in payload["html"] and "202" in payload["html"]


def test_endpoint_skips_the_ad_server_for_ineligible_buys():
    payload, status, sync = _payload(_buy(status="draft"), SimpleNamespace(principal_id="p_1"), None)

    assert status == 200
    sync.assert_not_called()
    assert payload["delivery_metrics"] is None
    assert "No delivery reported" in payload["html"]


def test_endpoint_reports_missing_buy():
    payload, status, sync = _payload(None, None, None)

    assert status == 404
    sync.assert_not_called()


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
