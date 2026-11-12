"""Regression tests: A2A auth must NOT use ContextVar fallback.

Core invariant: All auth context flows through explicit function parameters
(scope["state"] -> ServerCallContext -> handler methods), never through
ambient ContextVar state.

These tests verify the ContextVar fallback is removed from the A2A handler.
They FAIL before the refactoring (TDD red step) and PASS after.

beads: salesagent-zmb1
"""


class TestNoContextVarInMiddleware:
    """UnifiedAuthMiddleware must not write to _auth_context_var."""

    def test_middleware_module_does_not_export_auth_context_var(self):
        """auth_middleware module must not have _auth_context_var in its namespace."""
        import src.core.auth_middleware as mw_mod

        assert not hasattr(mw_mod, "_auth_context_var"), (
            "UnifiedAuthMiddleware still imports _auth_context_var. Only scope['state'] should be written to."
        )


class TestNoContextVarInfrastructure:
    """ContextVar infrastructure must not exist in auth_context module."""

    def test_no_auth_context_var_in_module(self):
        """auth_context.py must not define _auth_context_var."""
        import src.core.auth_context as ac_mod

        assert not hasattr(ac_mod, "_auth_context_var"), (
            "_auth_context_var still exists in auth_context module. Remove it after all consumers are migrated."
        )

    def test_no_get_current_auth_context_in_module(self):
        """auth_context.py must not define get_current_auth_context."""
        import src.core.auth_context as ac_mod

        assert not hasattr(ac_mod, "get_current_auth_context"), (
            "get_current_auth_context() still exists in auth_context module. "
            "Remove it after all consumers are migrated."
        )
