"""Anonymous discovery requests carry their tenant into the SDK dispatch task.

adcp 7 (fastmcp 4 / mcp 2) runs tool dispatch where the tenant-router
ContextVar is not visible; the context factory must pin the tenant itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from adcp.server.auth import current_tenant
from adcp.server.base import ToolContext

import core.main as core_main


def _anonymous_ctx() -> ToolContext:
    return ToolContext(request_id="req-1", caller_identity=None, tenant_id=None, metadata={})


def test_anonymous_request_pins_tenant_from_headers():
    meta = SimpleNamespace(
        request_context=SimpleNamespace(headers={"x-adcp-tenant": "default", "host": "localhost:8000"})
    )
    token = current_tenant.set(None)
    try:
        with (
            patch.object(core_main, "auth_context_factory", return_value=_anonymous_ctx()),
            patch.object(
                core_main, "get_principal_from_context", return_value=(None, {"tenant_id": "default"})
            ) as detect,
        ):
            ctx = core_main.auth_context_factory_with_discovery_fallback(meta)
        assert ctx.tenant_id == "default"
        assert ctx.caller_identity is None
        assert current_tenant.get() == "default"
        shim = detect.call_args.args[0]
        assert shim.headers["x-adcp-tenant"] == "default"
    finally:
        current_tenant.reset(token)


def test_anonymous_request_without_tenant_hint_is_unchanged():
    meta = SimpleNamespace(request_context=SimpleNamespace(headers={}))
    anonymous = _anonymous_ctx()
    with (
        patch.object(core_main, "auth_context_factory", return_value=anonymous),
        patch.object(core_main, "get_principal_from_context", return_value=(None, None)),
    ):
        assert core_main.auth_context_factory_with_discovery_fallback(meta) is anonymous
