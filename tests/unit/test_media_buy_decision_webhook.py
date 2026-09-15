"""Approval / rejection webhooks for media buys created with push_notification_config.

A ``create_media_buy`` request stores its ``push_notification_config`` on the
workflow step (request-scoped), never in ``push_notification_configs``. The
admin approve/reject handlers must therefore be able to rebuild a delivery
config from the step and send the terminal task-status webhook.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from adcp.types import McpWebhookPayload

from src.services.protocol_webhook_service import (
    build_request_scoped_config,
    send_create_media_buy_decision,
)

_PUSH_CONFIG = {
    "url": "http://127.0.0.1:9999/webhook",
    "operation_id": "op-1",
    "authentication": {"schemes": ["HMAC-SHA256"], "credentials": "s" * 40},
}


def test_build_request_scoped_config_from_step_config():
    cfg = build_request_scoped_config(tenant_id="t1", principal_id="p1", push_config=_PUSH_CONFIG)
    assert cfg is not None
    assert cfg.tenant_id == "t1"
    assert cfg.principal_id == "p1"
    assert cfg.url == _PUSH_CONFIG["url"]
    assert cfg.authentication_type == "HMAC-SHA256"
    assert cfg.authentication_token == "s" * 40
    assert cfg.purpose == "async_task"
    assert cfg.is_active is True


def test_build_request_scoped_config_without_authentication():
    cfg = build_request_scoped_config(tenant_id="t1", principal_id="p1", push_config={"url": "https://b.example/h"})
    assert cfg is not None
    assert cfg.authentication_type is None
    assert cfg.authentication_token is None


def test_build_request_scoped_config_returns_none_when_no_url():
    assert build_request_scoped_config(tenant_id="t1", principal_id="p1", push_config=None) is None
    assert build_request_scoped_config(tenant_id="t1", principal_id="p1", push_config={}) is None
    assert build_request_scoped_config(tenant_id="t1", principal_id="p1", push_config={"operation_id": "x"}) is None


def _run_decision(protocol: str, status: str):
    cfg = build_request_scoped_config(tenant_id="t1", principal_id="p1", push_config=_PUSH_CONFIG)
    service = MagicMock()
    service.send_notification = AsyncMock(return_value=True)
    with patch("src.services.protocol_webhook_service.get_protocol_webhook_service", return_value=service):
        ok = send_create_media_buy_decision(
            config=cfg,
            step_id="step_1",
            context_id="ctx_1",
            protocol=protocol,
            media_buy_id="mb_1",
            package_ids=["pkg_a", "pkg_b"],
            status=status,
        )
    assert ok is True
    service.send_notification.assert_awaited_once()
    return service.send_notification.await_args.kwargs


def test_send_decision_mcp_completed_payload():
    kwargs = _run_decision("mcp", "completed")
    payload = kwargs["payload"]
    assert isinstance(payload, McpWebhookPayload)
    assert payload.task_id == "step_1"
    assert payload.task_type.value == "create_media_buy"
    assert payload.status.value == "completed"
    dumped = payload.model_dump(mode="json", exclude_none=True)
    assert dumped["result"]["media_buy_id"] == "mb_1"
    assert [p["package_id"] for p in dumped["result"]["packages"]] == ["pkg_a", "pkg_b"]
    assert kwargs["metadata"]["task_type"] == "create_media_buy"
    assert kwargs["push_notification_config"].url == _PUSH_CONFIG["url"]


def test_send_decision_mcp_rejected_status():
    kwargs = _run_decision("mcp", "rejected")
    assert kwargs["payload"].status.value == "rejected"


def test_send_decision_a2a_payload_is_task_object():
    kwargs = _run_decision("a2a", "completed")
    payload = kwargs["payload"]
    assert not isinstance(payload, McpWebhookPayload)
    assert payload.id == "step_1"
    assert payload.context_id == "ctx_1"


def test_send_decision_returns_false_when_send_raises():
    cfg = build_request_scoped_config(tenant_id="t1", principal_id="p1", push_config=_PUSH_CONFIG)
    service = MagicMock()
    service.send_notification = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("src.services.protocol_webhook_service.get_protocol_webhook_service", return_value=service):
        ok = send_create_media_buy_decision(
            config=cfg,
            step_id="step_1",
            context_id="ctx_1",
            protocol="mcp",
            media_buy_id="mb_1",
            package_ids=[],
            status="completed",
        )
    assert ok is False
