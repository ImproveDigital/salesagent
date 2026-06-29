"""Unit tests for create_media_buy setup-check bypass conditions.

validate_setup_complete() is skipped when ANY of these three conditions holds:

  1. testing_ctx.dry_run is True
  2. testing_ctx.test_session_id is a non-empty string
  3. ADCP_SKIP_SETUP_CHECK=true environment variable is set

These tests pin those bypass gates so a future refactor that accidentally
removes or narrows one of the conditions is caught immediately. Each test
gives the function an identity that WOULD trigger setup validation on the
production path (dry_run=False, test_session_id=None, no env var override)
and verifies that the given bypass condition prevents the call.

Strategy: patch validate_setup_complete to raise if called, then assert it
wasn't called. If the bypass is working the function proceeds to a later
failure (product/currency DB lookup) — not the setup error.

Covered gaps:
  TC-SETUP-003 — ADCP_SKIP_SETUP_CHECK=true env var bypasses validate_setup_complete
  TC-SETUP-004 — dry_run=True bypasses validate_setup_complete
  TC-SETUP-005 — test_session_id set bypasses validate_setup_complete
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import CreateMediaBuyRequest
from src.core.testing_hooks import AdCPTestContext
from tests.factories.spec_required_kwargs import required_request_kwargs


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _future(days: int = 7) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _minimal_request() -> CreateMediaBuyRequest:
    return CreateMediaBuyRequest(
        **required_request_kwargs(),
        brand={"domain": "testbrand.com"},
        start_time=_future(1),
        end_time=_future(8),
        packages=[{"product_id": "prod_1", "budget": 5000.0, "pricing_option_id": "cpm_usd_fixed"}],
    )


def _production_identity(*, dry_run: bool = False, test_session_id: str | None = None) -> ResolvedIdentity:
    """Identity that would reach validate_setup_complete on the production path
    (i.e. dry_run=False and test_session_id=None) unless overridden."""
    return ResolvedIdentity(
        principal_id="principal_1",
        tenant_id="test_tenant",
        tenant={"tenant_id": "test_tenant", "human_review_required": False, "auto_create_media_buys": True},
        auth_token="test-token",
        protocol="mcp",
        testing_context=AdCPTestContext(dry_run=dry_run, test_session_id=test_session_id),
    )


def _standard_patches():
    """Return the minimal patches needed to reach the setup-check branch without
    crashing on principal/session lookups that follow it."""
    mock_principal = MagicMock()
    mock_principal.principal_id = "principal_1"
    mock_principal.platform_mappings = {}

    mock_step = MagicMock()
    mock_step.step_id = "step_1"
    mock_ctx_mgr = MagicMock()
    mock_ctx_mgr.create_context.return_value = MagicMock(context_id="ctx_1")
    mock_ctx_mgr.create_workflow_step.return_value = mock_step

    mock_media_buys_repo = MagicMock()
    mock_media_buys_repo.find_by_idempotency_key.return_value = None

    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=None)
    mock_uow.session = MagicMock()
    mock_uow.media_buys = mock_media_buys_repo

    return mock_principal, mock_ctx_mgr, mock_uow


_PATCH_PRINCIPAL = "src.core.tools.media_buy_create.get_principal_object"
_PATCH_UOW = "src.core.database.repositories.MediaBuyUoW"
_PATCH_CTX_MGR = "src.core.tools.media_buy_create.get_context_manager"
_PATCH_SANDBOX = "src.core.tools.media_buy_create.sandbox_mode_for_request"
_PATCH_SETUP = "src.core.tools.media_buy_create.validate_setup_complete"


# ===========================================================================
# TC-SETUP-003  ADCP_SKIP_SETUP_CHECK env var
# ===========================================================================


class TestEnvVarBypassSetupCheck:
    """TC-SETUP-003: ADCP_SKIP_SETUP_CHECK=true bypasses validate_setup_complete.

    WHY THIS TEST EXISTS:
    Production deployments and certain CI environments set this env var to skip
    the setup checklist (e.g. when seeding a blank tenant for the first time).
    The bypass must work even when the identity carries no test_session_id and
    dry_run is False — otherwise the function raises AdCPValidationError before
    any real work can proceed in those environments.
    """

    @pytest.mark.asyncio
    async def test_skip_env_var_prevents_setup_check(self):
        """TC-SETUP-003: ADCP_SKIP_SETUP_CHECK=true → validate_setup_complete not called."""
        from src.core.tools.media_buy_create import _create_media_buy_impl

        identity = _production_identity(dry_run=False, test_session_id=None)
        req = _minimal_request()
        mock_principal, mock_ctx_mgr, mock_uow = _standard_patches()

        with (
            patch.dict(os.environ, {"ADCP_SKIP_SETUP_CHECK": "true"}),
            patch(_PATCH_SETUP) as mock_setup,
            patch(_PATCH_PRINCIPAL, return_value=mock_principal),
            patch(_PATCH_UOW, return_value=mock_uow),
            patch(_PATCH_CTX_MGR, return_value=mock_ctx_mgr),
            patch(_PATCH_SANDBOX, return_value=MagicMock(active=False, diagnostic="")),
        ):
            # The function proceeds past setup check and fails later at product lookup
            # (mock DB returns no products). We only care that setup was NOT called.
            try:
                await _create_media_buy_impl(req=req, identity=identity)
            except Exception:
                pass

        mock_setup.assert_not_called(), (
            "validate_setup_complete must not be called when ADCP_SKIP_SETUP_CHECK=true."
        )

    @pytest.mark.asyncio
    async def test_without_env_var_setup_check_is_called(self):
        """TC-SETUP-003 complement: without the env var, validate_setup_complete IS called.

        WHY THIS TEST EXISTS:
        Pins the positive case so the env-var bypass test can't silently pass
        because setup validation was removed entirely. If setup is never called
        regardless of the env var, both tests would wrongly pass.
        """
        from src.services.setup_checklist_service import SetupIncompleteError
        from src.core.tools.media_buy_create import _create_media_buy_impl
        from src.core.exceptions import AdCPValidationError

        identity = _production_identity(dry_run=False, test_session_id=None)
        req = _minimal_request()
        mock_principal, _, _ = _standard_patches()

        with (
            patch.dict(os.environ, {}, clear=False),  # ensure env var is absent
            patch.dict(os.environ, {"ADCP_SKIP_SETUP_CHECK": ""}),  # empty string = falsy
            patch(_PATCH_SETUP, side_effect=SetupIncompleteError(
                "Incomplete", missing_tasks=[{"name": "Products", "description": "Add products"}]
            )),
            patch(_PATCH_PRINCIPAL, return_value=mock_principal),
        ):
            with pytest.raises(AdCPValidationError, match="Setup incomplete"):
                await _create_media_buy_impl(req=req, identity=identity)


# ===========================================================================
# TC-SETUP-004  dry_run=True bypass
# ===========================================================================


class TestDryRunBypassSetupCheck:
    """TC-SETUP-004: dry_run=True bypasses validate_setup_complete.

    WHY THIS TEST EXISTS:
    dry_run mode is used for testing without side effects (no DB writes, no adapter
    calls). The setup check is skipped because dry-run callers may be testing against
    an intentionally incomplete tenant. If dry_run didn't bypass setup validation,
    every dry-run test would need a fully-configured tenant, defeating the purpose
    of the lightweight dry-run mode.
    """

    @pytest.mark.asyncio
    async def test_dry_run_prevents_setup_check(self):
        """TC-SETUP-004: testing_ctx.dry_run=True → validate_setup_complete not called."""
        from src.core.tools.media_buy_create import _create_media_buy_impl

        # dry_run=True — setup check must be skipped
        identity = _production_identity(dry_run=True, test_session_id=None)
        req = _minimal_request()
        mock_principal, _, mock_uow = _standard_patches()

        with (
            patch(_PATCH_SETUP) as mock_setup,
            patch(_PATCH_PRINCIPAL, return_value=mock_principal),
            patch(_PATCH_UOW, return_value=mock_uow),
            patch(_PATCH_SANDBOX, return_value=MagicMock(active=False, diagnostic="")),
        ):
            try:
                await _create_media_buy_impl(req=req, identity=identity)
            except Exception:
                pass  # dry_run may short-circuit differently; we only care setup was skipped

        mock_setup.assert_not_called(), (
            "validate_setup_complete must not be called when dry_run=True."
        )


# ===========================================================================
# TC-SETUP-005  test_session_id set
# ===========================================================================


class TestSessionIdBypassSetupCheck:
    """TC-SETUP-005: test_session_id set bypasses validate_setup_complete.

    WHY THIS TEST EXISTS:
    test_session_id is the mechanism used by automated tests to run against an
    intentionally minimal tenant (e.g. one without the full setup checklist
    completed). Without this bypass, every unit test that calls _create_media_buy_impl
    would need to mock validate_setup_complete as well. This test pins the bypass
    explicitly so it is not accidentally removed when the setup-check logic is refactored.
    """

    @pytest.mark.asyncio
    async def test_test_session_id_prevents_setup_check(self):
        """TC-SETUP-005: test_session_id='anything' → validate_setup_complete not called."""
        from src.core.tools.media_buy_create import _create_media_buy_impl

        # test_session_id is set — setup check must be skipped
        identity = _production_identity(dry_run=False, test_session_id="my-test-session-abc")
        req = _minimal_request()
        mock_principal, mock_ctx_mgr, mock_uow = _standard_patches()

        with (
            patch(_PATCH_SETUP) as mock_setup,
            patch(_PATCH_PRINCIPAL, return_value=mock_principal),
            patch(_PATCH_UOW, return_value=mock_uow),
            patch(_PATCH_CTX_MGR, return_value=mock_ctx_mgr),
            patch(_PATCH_SANDBOX, return_value=MagicMock(active=False, diagnostic="")),
        ):
            try:
                await _create_media_buy_impl(req=req, identity=identity)
            except Exception:
                pass  # fails at product lookup later — only care that setup was skipped

        mock_setup.assert_not_called(), (
            "validate_setup_complete must not be called when test_session_id is set."
        )
