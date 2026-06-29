"""Unit tests for create_media_buy request-level validation.

These tests exercise the validation logic inside _create_media_buy_impl that runs
after authentication and before any adapter call. All failures here are caught by
the inner `except (ValueError, PermissionError)` block and returned as
CreateMediaBuyError results — they do NOT propagate as Python exceptions.

Exception: TC-BUDG-003 verifies that sandbox mode passes the budget gate and
reaches product validation (which raises AdCPProductNotFoundError — an uncaught
AdCPError that propagates). The test asserts the budget check was NOT the failure.

Minimal patching strategy (4 shared patches):
  - get_principal_object     → stub principal (avoids DB lookup)
  - MediaBuyUoW              → mock UoW (idempotency miss + empty product list)
  - get_context_manager      → stub (workflow step creation)
  - sandbox_mode_for_request → stub returning active=False (overridden per-test)

test_session_id="unit-test" in the identity bypasses validate_setup_complete,
so no separate patch for setup is needed.

Covered gaps (not in any existing test as of this writing):
  TC-DATE-002 — past start_time is rejected at impl level (not schema level)
  TC-DATE-003 — end_time before start_time rejected at impl level
  TC-DATE-004 — end_time equal to start_time rejected (boundary: must be strictly after)
  TC-PROD-001 — empty packages list → "At least one product is required"
  TC-PROD-003 — duplicate product_id across packages → descriptive error listing IDs
  TC-BUDG-002 — negative total budget rejected in non-sandbox mode
  TC-BUDG-003 — sandbox mode: zero budget passes budget gate (< 0, not <= 0)
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.core.exceptions import AdCPProductNotFoundError
from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import CreateMediaBuyError, CreateMediaBuyRequest
from src.core.testing_hooks import AdCPTestContext
from tests.factories.spec_required_kwargs import required_request_kwargs


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _future(days: int = 7) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _past(days: int = 1) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _make_identity(*, tenant: dict | None = None) -> ResolvedIdentity:
    """Minimal ResolvedIdentity that passes auth guards.

    test_session_id='unit-test' bypasses validate_setup_complete so no patch is needed.
    """
    return ResolvedIdentity(
        principal_id="principal_1",
        tenant_id="test_tenant",
        tenant=tenant or {
            "tenant_id": "test_tenant",
            "human_review_required": False,
            "auto_create_media_buys": True,
        },
        auth_token="test-token",
        protocol="mcp",
        testing_context=AdCPTestContext(dry_run=False, test_session_id="unit-test"),
    )


def _make_request(**overrides) -> CreateMediaBuyRequest:
    """Minimal valid CreateMediaBuyRequest; override any field via kwargs."""
    defaults = {
        **required_request_kwargs(),
        "brand": {"domain": "testbrand.com"},
        "start_time": _future(1),
        "end_time": _future(8),
        "packages": [
            {
                "product_id": "prod_1",
                "budget": 5000.0,
                "pricing_option_id": "cpm_usd_fixed",
            }
        ],
    }
    defaults.update(overrides)
    return CreateMediaBuyRequest(**defaults)


def _build_mock_uow(*, products_in_db: list | None = None) -> MagicMock:
    """Build a mock MediaBuyUoW that returns no idempotency hit and configurable products."""
    mock_media_buys_repo = MagicMock()
    mock_media_buys_repo.find_by_idempotency_key.return_value = None  # no replay

    mock_session = MagicMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = products_in_db or []
    scalars_result.first.return_value = None  # no currency_limit / adapter_config
    mock_session.scalars.return_value = scalars_result

    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=None)
    mock_uow.session = mock_session
    mock_uow.media_buys = mock_media_buys_repo
    return mock_uow


def _build_mock_ctx_manager() -> MagicMock:
    mock_step = MagicMock()
    mock_step.step_id = "step_1"
    mgr = MagicMock()
    mgr.create_context.return_value = MagicMock(context_id="ctx_1")
    mgr.create_workflow_step.return_value = mock_step
    return mgr


def _build_mock_principal() -> MagicMock:
    p = MagicMock()
    p.principal_id = "principal_1"
    p.platform_mappings = {}
    return p


# ---------------------------------------------------------------------------
# Shared patch targets
# ---------------------------------------------------------------------------

_PATCH_PRINCIPAL = "src.core.tools.media_buy_create.get_principal_object"
_PATCH_UOW = "src.core.database.repositories.MediaBuyUoW"
_PATCH_CTX_MGR = "src.core.tools.media_buy_create.get_context_manager"
_PATCH_SANDBOX = "src.core.tools.media_buy_create.sandbox_mode_for_request"


# ===========================================================================
# TC-DATE-002, 003, 004  —  DateTime validation
# ===========================================================================


class TestDateTimeValidation:
    """Tests that _create_media_buy_impl rejects invalid flight windows.

    AdCP requires:
      - start_time must not be in the past
      - end_time must be strictly after start_time

    The Pydantic schema does NOT enforce the past-start or end>start rules;
    that validation is done at the impl level. These tests pin the impl-level
    check so a future refactor that removes it (leaving only schema-level
    validation) is caught.

    All three tests expect the function to RETURN a CreateMediaBuyError result
    (not raise) because ValueError is caught internally and converted to an error
    response.
    """

    @pytest.mark.asyncio
    async def test_past_start_time_returns_invalid_request_error(self):
        """TC-DATE-002: start_time in the past → CreateMediaBuyError("invalid_request").

        WHY THIS TEST EXISTS:
        A media buy with a past start_time would immediately enter 'active' status on
        creation with a backdated flight, confusing both the buyer (who expects
        'pending_start') and the ad server (which may reject or misplace the order).
        The impl rejects it with a descriptive error so the buyer can correct their
        dates and retry with a fresh idempotency_key.
        """
        from src.core.tools.media_buy_create import _create_media_buy_impl

        req = _make_request(
            start_time=_past(days=2),  # 2 days ago — clearly in the past
            end_time=_future(days=8),
        )
        identity = _make_identity()

        with (
            patch(_PATCH_PRINCIPAL, return_value=_build_mock_principal()),
            patch(_PATCH_UOW, return_value=_build_mock_uow()),
            patch(_PATCH_CTX_MGR, return_value=_build_mock_ctx_manager()),
            patch(_PATCH_SANDBOX, return_value=MagicMock(active=False, diagnostic="")),
        ):
            result = await _create_media_buy_impl(req=req, identity=identity)

        assert isinstance(result.response, CreateMediaBuyError), (
            "Past start_time must produce a CreateMediaBuyError, not a success."
        )
        error_message = result.response.errors[0].message
        assert "Start time cannot be in the past" in error_message, (
            f"Error message must explain the problem clearly. Got: {error_message!r}"
        )

    @pytest.mark.asyncio
    async def test_end_time_before_start_time_returns_invalid_request_error(self):
        """TC-DATE-003: end_time < start_time → CreateMediaBuyError("invalid_request").

        WHY THIS TEST EXISTS:
        The Pydantic schema accepts reversed dates (it only enforces types, not ordering).
        Without the impl-level guard, a buy with end_time < start_time would be created
        with a zero-length or negative-length flight, causing the status function to
        immediately return 'completed' on creation. This test ensures the impl rejects
        the bad input before writing anything to the DB.
        """
        from src.core.tools.media_buy_create import _create_media_buy_impl

        req = _make_request(
            start_time=_future(days=5),
            end_time=_future(days=2),  # before start
        )
        identity = _make_identity()

        with (
            patch(_PATCH_PRINCIPAL, return_value=_build_mock_principal()),
            patch(_PATCH_UOW, return_value=_build_mock_uow()),
            patch(_PATCH_CTX_MGR, return_value=_build_mock_ctx_manager()),
            patch(_PATCH_SANDBOX, return_value=MagicMock(active=False, diagnostic="")),
        ):
            result = await _create_media_buy_impl(req=req, identity=identity)

        assert isinstance(result.response, CreateMediaBuyError)
        error_message = result.response.errors[0].message
        assert "must be after start time" in error_message, (
            f"Error must explain the ordering constraint. Got: {error_message!r}"
        )

    @pytest.mark.asyncio
    async def test_end_time_equal_to_start_time_returns_invalid_request_error(self):
        """TC-DATE-004: end_time == start_time → CreateMediaBuyError.

        WHY THIS TEST EXISTS:
        The check is `computed_end_time <= computed_start_time` (strictly after).
        A zero-length flight (same second for start and end) is also invalid — a
        media buy needs at least some duration to serve impressions. This boundary
        test pins the <= operator so a refactor changing it to < would be caught.
        """
        from src.core.tools.media_buy_create import _create_media_buy_impl

        same_time = _future(days=3)
        req = _make_request(
            start_time=same_time,
            end_time=same_time,  # exactly equal
        )
        identity = _make_identity()

        with (
            patch(_PATCH_PRINCIPAL, return_value=_build_mock_principal()),
            patch(_PATCH_UOW, return_value=_build_mock_uow()),
            patch(_PATCH_CTX_MGR, return_value=_build_mock_ctx_manager()),
            patch(_PATCH_SANDBOX, return_value=MagicMock(active=False, diagnostic="")),
        ):
            result = await _create_media_buy_impl(req=req, identity=identity)

        assert isinstance(result.response, CreateMediaBuyError), (
            "A zero-length flight (start == end) must be rejected, not accepted."
        )


# ===========================================================================
# TC-PROD-001, TC-PROD-003  —  Package / product validation
# ===========================================================================


class TestPackageValidation:
    """Tests that _create_media_buy_impl rejects invalid package configurations.

    TC-PROD-001: no packages → "At least one product is required."
    TC-PROD-003: duplicate product_id across packages → descriptive error.

    TC-PROD-001 uses sandbox mode because empty packages means total_budget=0,
    which fails the non-sandbox budget check before reaching package validation.
    Sandbox uses a < 0 threshold so zero budget is allowed, and the 'no products'
    check is reached.
    """

    @pytest.mark.asyncio
    async def test_empty_packages_returns_at_least_one_product_error(self):
        """TC-PROD-001: packages=[] → CreateMediaBuyError("At least one product is required").

        WHY THIS TEST EXISTS:
        The impl derives product_ids from packages. If packages is empty, there are
        no product IDs and no budget to validate against, making the buy meaningless.
        This guard fires early so the buyer sees a clear "no products" error instead
        of a confusing downstream failure (e.g. "no pricing found" or "empty adapter request").

        Sandbox mode is used so the zero-budget check (total_budget=0 from empty packages)
        does not fire first and mask the packages error.
        """
        from src.core.tools.media_buy_create import _create_media_buy_impl

        req = _make_request(packages=[])  # no packages → total_budget = 0
        identity = _make_identity()

        with (
            patch(_PATCH_PRINCIPAL, return_value=_build_mock_principal()),
            patch(_PATCH_UOW, return_value=_build_mock_uow()),
            patch(_PATCH_CTX_MGR, return_value=_build_mock_ctx_manager()),
            # Sandbox mode: budget < 0 check (not <= 0), so zero budget passes the gate.
            patch(_PATCH_SANDBOX, return_value=MagicMock(active=True, diagnostic="sandbox")),
        ):
            result = await _create_media_buy_impl(req=req, identity=identity)

        assert isinstance(result.response, CreateMediaBuyError)
        error_message = result.response.errors[0].message
        assert "At least one product is required" in error_message, (
            f"Error must clearly state that products are missing. Got: {error_message!r}"
        )

    @pytest.mark.asyncio
    async def test_duplicate_product_id_returns_descriptive_error(self):
        """TC-PROD-003: two packages with the same product_id → error naming the duplicate IDs.

        WHY THIS TEST EXISTS:
        Buyers occasionally copy-paste package configs and forget to change the product_id.
        Without this check, the duplicate would create two line items for the same product
        in the ad server, causing over-delivery or budget conflicts. The error names the
        duplicate IDs so the buyer can fix the request precisely.

        The impl allows the same product_id in get_product_ids (deduplicates to a set),
        but then loops through packages counting occurrences and rejects any with count > 1.
        """
        from src.core.tools.media_buy_create import _create_media_buy_impl

        req = _make_request(
            packages=[
                {"product_id": "prod_video", "budget": 3000.0, "pricing_option_id": "cpm_usd"},
                {"product_id": "prod_video", "budget": 2000.0, "pricing_option_id": "cpm_usd"},
            ]
        )
        identity = _make_identity()

        with (
            patch(_PATCH_PRINCIPAL, return_value=_build_mock_principal()),
            patch(_PATCH_UOW, return_value=_build_mock_uow()),
            patch(_PATCH_CTX_MGR, return_value=_build_mock_ctx_manager()),
            patch(_PATCH_SANDBOX, return_value=MagicMock(active=False, diagnostic="")),
        ):
            result = await _create_media_buy_impl(req=req, identity=identity)

        assert isinstance(result.response, CreateMediaBuyError)
        error_message = result.response.errors[0].message
        assert "Duplicate product_id" in error_message, (
            f"Error must name the problem as a duplicate. Got: {error_message!r}"
        )
        assert "prod_video" in error_message, (
            "Error must name the offending product_id so the buyer can fix it."
        )


# ===========================================================================
# TC-BUDG-002, TC-BUDG-003  —  Budget boundary validation
# ===========================================================================


class TestBudgetBoundaryValidation:
    """Tests for budget checks at the boundary between valid and invalid.

    TC-BUDG-002: negative budget is always invalid (non-sandbox mode).
    TC-BUDG-003: sandbox mode uses `< 0` threshold instead of `<= 0`, so
                 a budget of exactly 0 passes the gate (no ad spend in sandbox).

    The budget check code:
      if sandbox_mode.active:
          budget_invalid = total_budget < 0   ← TC-BUDG-003 pins this branch
      else:
          budget_invalid = total_budget <= 0  ← TC-BUDG-002 pins this branch
    """

    @pytest.mark.asyncio
    async def test_zero_budget_non_sandbox_returns_invalid_budget_error(self):
        """TC-BUDG-002: total_budget = 0 in non-sandbox mode → CreateMediaBuyError.

        WHY THIS TEST EXISTS:
        The adcp library schema enforces `budget >= 0` at the Pydantic level, so
        negative values are rejected before reaching the impl. The impl-level check
        catches the remaining gap: `total_budget <= 0` (strict positive required).
        A per-package budget of 0.0 is a valid Pydantic value but an invalid media buy
        — there is nothing to spend. This test pins the impl-level `<= 0` check so a
        future refactor that removes or weakens it is caught.
        """
        from src.core.tools.media_buy_create import _create_media_buy_impl

        req = _make_request(
            packages=[
                {"product_id": "prod_1", "budget": 0.0, "pricing_option_id": "cpm_usd"}
            ]
        )
        identity = _make_identity()

        with (
            patch(_PATCH_PRINCIPAL, return_value=_build_mock_principal()),
            patch(_PATCH_UOW, return_value=_build_mock_uow()),
            patch(_PATCH_CTX_MGR, return_value=_build_mock_ctx_manager()),
            patch(_PATCH_SANDBOX, return_value=MagicMock(active=False, diagnostic="")),
        ):
            result = await _create_media_buy_impl(req=req, identity=identity)

        assert isinstance(result.response, CreateMediaBuyError)
        error_message = result.response.errors[0].message
        assert "Budget must be positive" in error_message, (
            f"Non-sandbox zero budget must be rejected as 'must be positive'. Got: {error_message!r}"
        )

    @pytest.mark.asyncio
    async def test_sandbox_mode_allows_zero_budget_and_proceeds_to_product_validation(self):
        """TC-BUDG-003: sandbox mode uses `total_budget < 0` so budget=0 passes the gate.

        WHY THIS TEST EXISTS:
        In sandbox mode, zero economics are used to allow end-to-end test flows without
        charging real money. budget=0 is a valid sandbox request. The budget check must
        NOT reject it. This test verifies that a sandbox buy with budget=0 passes the
        budget gate and proceeds to product validation (failing there because the mock
        DB has no products). The failure mode (AdCPProductNotFoundError) proves the
        budget gate was cleared — if the budget gate had triggered, the function would
        have returned CreateMediaBuyError, not raised AdCPProductNotFoundError.
        """
        from src.core.tools.media_buy_create import _create_media_buy_impl

        req = _make_request(
            packages=[
                {"product_id": "prod_sandbox", "budget": 0.0, "pricing_option_id": "cpm_usd"}
            ]
        )
        identity = _make_identity()

        with (
            patch(_PATCH_PRINCIPAL, return_value=_build_mock_principal()),
            patch(_PATCH_UOW, return_value=_build_mock_uow(products_in_db=[])),
            patch(_PATCH_CTX_MGR, return_value=_build_mock_ctx_manager()),
            # Sandbox active — budget=0 should pass via `< 0` check.
            patch(_PATCH_SANDBOX, return_value=MagicMock(active=True, diagnostic="sandbox-mode")),
        ):
            # AdCPProductNotFoundError is an AdCPError (not ValueError), so it is NOT caught
            # by the inner except (ValueError, PermissionError) block. It propagates, which
            # proves the budget gate was cleared successfully.
            with pytest.raises(AdCPProductNotFoundError):
                await _create_media_buy_impl(req=req, identity=identity)
