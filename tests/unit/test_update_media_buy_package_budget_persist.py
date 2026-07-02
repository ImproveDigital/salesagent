"""Regression test: package-level budget updates must persist to our DB.

Pre-fix, update_media_buy with a package budget change called the adapter
and returned UpdateMediaBuySuccess with changes_applied, but never wrote
MediaPackage.budget — get_media_buys kept reporting the old value (silent
no-op / data loss). This drives _update_media_buy_impl through the package
budget path and asserts the repository persistence calls fire.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import UpdateMediaBuyRequest, UpdateMediaBuySuccess
from src.core.testing_hooks import AdCPTestContext
from src.core.tools.media_buy_update import _update_media_buy_impl
from tests.factories.spec_required_kwargs import required_request_kwargs

MODULE = "src.core.tools.media_buy_update"
MB = "mb_1"
PKG = "pkg_1"


def _identity():
    return ResolvedIdentity(
        principal_id="p_1",
        tenant_id="t_1",
        tenant={"tenant_id": "t_1", "name": "Test"},
        testing_context=AdCPTestContext(dry_run=False),
    )


def _buy():
    return MagicMock(
        media_buy_id=MB,
        principal_id="p_1",
        external_id=None,
        approved_at=None,
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        end_time=datetime(2026, 12, 31, tzinfo=UTC),
        currency="EUR",
        source="adcp",
        status="active",
    )


def _package():
    pkg = MagicMock(package_id=PKG)
    pkg.package_config = {"product_id": "prod_1", "budget": {"total": 1.0, "currency": "EUR"}}
    pkg.budget = 1.0
    return pkg


@pytest.fixture
def env():
    uow = MagicMock()
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)
    uow.session = MagicMock()
    uow.media_buys = MagicMock()
    uow.media_buys.find_by_idempotency_key.return_value = None
    uow.media_buys.get_by_id.return_value = _buy()
    uow.media_buys.get_by_id_for_update.return_value = _buy()
    uow.media_buys.get_package.return_value = _package()
    uow.media_buys.get_packages.return_value = [_package()]
    # No currency constraints in the way of the budget update.
    uow.currency_limits = MagicMock()
    uow.currency_limits.get_for_currency.return_value = MagicMock(min_package_budget=None, max_daily_package_spend=None)

    adapter = MagicMock()
    adapter.manual_approval_required = False
    adapter.manual_approval_operations = []
    adapter.update_media_buy.return_value = MagicMock(affected_packages=[])  # not an UpdateMediaBuyError

    with (
        patch(f"{MODULE}.MediaBuyUoW", return_value=uow),
        patch(f"{MODULE}.get_principal_object", return_value=MagicMock(principal_id="p_1")),
        patch(f"{MODULE}._verify_principal"),
        patch(f"{MODULE}.get_context_manager") as m_ctx,
        patch(f"{MODULE}.get_adapter", return_value=adapter),
        patch(f"{MODULE}.is_projected_media_buy_id", return_value=False),
        patch(f"{MODULE}.get_audit_logger", return_value=MagicMock()),
        patch("src.core.helpers.context_helpers.ensure_tenant_context", return_value={"tenant_id": "t_1"}),
        patch("src.core.database.repositories.workflow.WorkflowRepository") as m_repo,
    ):
        m_ctx.return_value.get_or_create_context.return_value = MagicMock(context_id="ctx_1")
        m_ctx.return_value.create_workflow_step.return_value = MagicMock(step_id="step_1")
        m_repo.return_value.find_by_idempotency_key.return_value = None
        yield {"uow": uow, "adapter": adapter}


def test_package_budget_update_persists_to_db(env):
    req = UpdateMediaBuyRequest(
        **required_request_kwargs(idempotency_key="pkgbudget-key-123456"),
        media_buy_id=MB,
        packages=[{"package_id": PKG, "budget": 9}],
    )

    result = _update_media_buy_impl(req=req, identity=_identity(), bypass_manual_approval=True)

    assert isinstance(result, UpdateMediaBuySuccess)
    # The MediaPackage.budget column is what get_media_buys reads back.
    env["uow"].media_buys.update_package_fields.assert_called_once_with(MB, PKG, budget=9.0)
    # package_config.budget is kept consistent too.
    env["uow"].media_buys.update_package_config.assert_called_once()
    cfg_args = env["uow"].media_buys.update_package_config.call_args.args
    assert cfg_args[0] == MB and cfg_args[1] == PKG
    assert cfg_args[2]["budget"] == {"total": 9.0, "currency": "EUR"}
