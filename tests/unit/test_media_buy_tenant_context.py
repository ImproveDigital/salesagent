"""Test that media_buy_create passes serialized tenant context downstream.

Bug history: execute_approved_media_buy() once manually constructed a partial tenant
dict with only a few fields. Passing the ORM model fixed that lossiness, but nested
session isolation means detached ORM tenants must not escape the UoW either. The
contract now is: serialize the ORM model through the central tenant serializer and
pass that full dict to downstream adapter/feature-flag helpers.
"""

import ast
import pathlib

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


class TestMediaBuyTenantContext:
    """Verify execute_approved_media_buy passes full tenant context downstream."""

    def test_no_manual_tenant_dict_construction(self):
        """execute_approved_media_buy should not manually construct tenant dicts.

        Manual dict construction (tenant_dict = {"tenant_id": ..., "name": ...}) misses
        fields and violates the central serialization rule.
        """
        source = (_PROJECT_ROOT / "src" / "core" / "tools" / "media_buy_create.py").read_text()

        tree = ast.parse(source)

        func_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "execute_approved_media_buy":
                func_node = node
                break

        assert func_node is not None, "execute_approved_media_buy function not found"

        func_source = ast.get_source_segment(source, func_node)
        assert func_source is not None

        # The bug pattern: manually building a dict with tenant fields
        assert "tenant_dict = {" not in func_source, (
            "execute_approved_media_buy manually constructs tenant_dict instead of using serialize_tenant_to_dict"
        )

    def test_passes_tenant_context_to_adapter_calls(self):
        """Downstream adapter calls should receive serialized tenant context."""
        source = (_PROJECT_ROOT / "src" / "core" / "tools" / "media_buy_create.py").read_text()

        tree = ast.parse(source)

        func_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "execute_approved_media_buy":
                func_node = node
                break

        assert func_node is not None
        func_source = ast.get_source_segment(source, func_node)
        assert func_source is not None

        assert "serialize_tenant_to_dict" in func_source
        assert "tenant_context = get_tenant_by_id(tenant_id) or serialize_tenant_to_dict(tenant_obj)" in func_source
        assert "tenant=tenant_obj" not in func_source, (
            "execute_approved_media_buy passes detached tenant ORM objects downstream; "
            "use serialized tenant_context instead"
        )

    def test_get_adapter_handles_serialized_tenant_dict(self):
        """get_adapter should accept serialized tenant dicts."""
        source = (_PROJECT_ROOT / "src" / "core" / "helpers" / "adapter_helpers.py").read_text()

        assert 'tenant["tenant_id"]' in source, "get_adapter does not support serialized tenant dict access"
        assert 'tenant.get("ad_server", "mock")' in source, (
            "get_adapter does not support serialized tenant dict access for ad_server"
        )
