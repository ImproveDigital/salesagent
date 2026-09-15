"""Workflows-page approve/reject must notify the buyer of the terminal task status.

``_notify_media_buy_decision`` is the shared helper behind both handlers in
``src/admin/blueprints/workflows.py``. It rebuilds the delivery config from
the step's request-scoped ``push_notification_config`` and sends the
``completed`` / ``rejected`` webhook.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from src.admin.blueprints.workflows import _notify_media_buy_decision

_PUSH_CONFIG = {
    "url": "http://127.0.0.1:9999/webhook",
    "authentication": {"schemes": ["Bearer"], "credentials": "t" * 40},
}


def _make_step(request_data):
    step = MagicMock()
    step.step_id = "step_1"
    step.context_id = "ctx_1"
    step.request_data = request_data
    return step


def _make_media_buy():
    mb = MagicMock()
    mb.media_buy_id = "mb_1"
    mb.principal_id = "p_1"
    mb.confirmed_at = datetime(2026, 9, 1, tzinfo=UTC)
    mb.revision = 3
    return mb


def _make_repo(package_ids):
    repo = MagicMock()
    repo.get_packages.return_value = [MagicMock(package_id=pid) for pid in package_ids]
    return repo


def test_rejected_decision_sends_webhook_from_step_config():
    step = _make_step({"push_notification_config": _PUSH_CONFIG, "protocol": "a2a"})
    with patch("src.admin.blueprints.workflows.send_create_media_buy_decision", return_value=True) as send:
        ok = _notify_media_buy_decision(step, "t_1", _make_media_buy(), _make_repo(["pkg_1"]), status="rejected")

    assert ok is True
    send.assert_called_once()
    kwargs = send.call_args.kwargs
    assert kwargs["status"] == "rejected"
    assert kwargs["protocol"] == "a2a"
    assert kwargs["step_id"] == "step_1"
    assert kwargs["context_id"] == "ctx_1"
    assert kwargs["media_buy_id"] == "mb_1"
    assert kwargs["package_ids"] == ["pkg_1"]
    assert kwargs["revision"] == 3
    assert kwargs["confirmed_at"] == datetime(2026, 9, 1, tzinfo=UTC)
    config = kwargs["config"]
    assert config.tenant_id == "t_1"
    assert config.principal_id == "p_1"
    assert config.url == _PUSH_CONFIG["url"]
    assert config.authentication_type == "Bearer"


def test_completed_decision_defaults_protocol_to_mcp():
    step = _make_step({"push_notification_config": _PUSH_CONFIG})
    with patch("src.admin.blueprints.workflows.send_create_media_buy_decision", return_value=True) as send:
        _notify_media_buy_decision(step, "t_1", _make_media_buy(), _make_repo([]), status="completed")

    assert send.call_args.kwargs["protocol"] == "mcp"
    assert send.call_args.kwargs["status"] == "completed"


def test_no_push_config_sends_nothing():
    step = _make_step({"protocol": "mcp"})
    with patch("src.admin.blueprints.workflows.send_create_media_buy_decision") as send:
        ok = _notify_media_buy_decision(step, "t_1", _make_media_buy(), _make_repo([]), status="rejected")

    assert ok is False
    send.assert_not_called()


def test_none_request_data_sends_nothing():
    step = _make_step(None)
    with patch("src.admin.blueprints.workflows.send_create_media_buy_decision") as send:
        ok = _notify_media_buy_decision(step, "t_1", _make_media_buy(), _make_repo([]), status="rejected")

    assert ok is False
    send.assert_not_called()
