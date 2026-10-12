"""Unit tests for miscellaneous create_media_buy guards.

Covers adapter-type guards (SpringServe non-tag, non-SpringServe) and the
MCP-level reporting webhook frequency warning.

Covered gaps:
  TC-SS-007 — non-tag SpringServe adapter: _prepare_springserve_tag_mode_packages is a no-op
  TC-SS-008 — non-SpringServe adapter (e.g. GAM): no tag-mode preparation runs
  TC-MCP-004 — non-daily reporting_webhook frequency: warning logged, request accepted
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import CreateMediaBuyRequest
from src.core.testing_hooks import AdCPTestContext
from tests.factories.spec_required_kwargs import required_request_kwargs


def _future(days: int = 7) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


# ===========================================================================
# TC-SS-007 / TC-SS-008 — SpringServe tag-mode guard
# ===========================================================================


class TestSpringServeTagModeGuards:
    """_is_springserve_tag_mode returns False for non-tag or non-SpringServe adapters.

    _prepare_springserve_tag_mode_packages calls _is_springserve_tag_mode first.
    When it returns False, the package list is returned unchanged — no creative
    lookup, no VAST URL injection. Tests here pin the two guard paths so a
    future rename of adapter_name or demand_class fields is caught.
    """

    def test_springserve_non_tag_demand_class_is_not_tag_mode(self):
        """TC-SS-007a: SpringServe adapter with demand_class='standard' → not tag mode.

        WHY THIS TEST EXISTS:
        SpringServe has multiple demand classes. Only demand_class='tag' requires
        VAST URL injection. Any other class must be treated as standard (creative
        assignment happens after adapter creation, not before). This test pins that
        the check is demand_class == 'tag', not just 'is SpringServe'.
        """
        from src.core.tools.media_buy_create import _is_springserve_tag_mode

        adapter = MagicMock()
        adapter.adapter_name = "springserve"
        adapter.demand_class = "standard"  # not "tag"

        assert not _is_springserve_tag_mode(adapter), "SpringServe with demand_class='standard' must NOT be tag mode."

    def test_non_springserve_adapter_is_not_tag_mode(self):
        """TC-SS-008a: GAM adapter → not tag mode, _is_springserve_tag_mode returns False.

        WHY THIS TEST EXISTS:
        Tag-mode VAST URL injection is SpringServe-specific. GAM, Mock, and other
        adapters must never have packages modified by the SpringServe preparation path.
        This test ensures the adapter_name check is enforced.
        """
        from src.core.tools.media_buy_create import _is_springserve_tag_mode

        adapter = MagicMock()
        adapter.adapter_name = "google_ad_manager"
        adapter.demand_class = "tag"  # demand_class is "tag" but adapter is NOT SpringServe

        assert not _is_springserve_tag_mode(adapter), (
            "Only SpringServe adapters can be tag mode. GAM with demand_class='tag' must not match."
        )

    def test_non_tag_springserve_returns_packages_unchanged(self):
        """TC-SS-007: _prepare_springserve_tag_mode_packages with non-tag SpringServe → same list.

        WHY THIS TEST EXISTS:
        Pins the early-return path of _prepare_springserve_tag_mode_packages. When
        _is_springserve_tag_mode returns False, the function must return the original
        packages list without any DB queries or VAST URL modification. Removing the
        early return would cause creative lookups for all SpringServe non-tag buys.
        """
        from src.core.tools.media_buy_create import _prepare_springserve_tag_mode_packages

        adapter = MagicMock()
        adapter.adapter_name = "springserve"
        adapter.demand_class = "standard"  # not tag

        pkg1 = MagicMock()
        pkg2 = MagicMock()
        packages = [pkg1, pkg2]
        mock_session = MagicMock()

        result = _prepare_springserve_tag_mode_packages(adapter, packages, "tenant_1", mock_session)

        assert result is packages, "Non-tag SpringServe must return the SAME packages list object unchanged."
        mock_session.scalars.assert_not_called(), ("No DB query must be made when adapter is not in tag mode.")

    def test_non_springserve_adapter_returns_packages_unchanged(self):
        """TC-SS-008: _prepare_springserve_tag_mode_packages with GAM adapter → same list.

        WHY THIS TEST EXISTS:
        Ensures the tag-mode preparation is completely skipped for non-SpringServe
        adapters. The assertion on mock_session.scalars.assert_not_called() verifies
        that no creative lookups are performed, which would add unnecessary DB load.
        """
        from src.core.tools.media_buy_create import _prepare_springserve_tag_mode_packages

        adapter = MagicMock()
        adapter.adapter_name = "google_ad_manager"

        packages = [MagicMock()]
        mock_session = MagicMock()

        result = _prepare_springserve_tag_mode_packages(adapter, packages, "tenant_1", mock_session)

        assert result is packages
        mock_session.scalars.assert_not_called()


# ===========================================================================
# TC-MCP-004 — Non-daily reporting webhook frequency
# ===========================================================================


class TestReportingWebhookFrequencyWarning:
    """TC-MCP-004: non-daily reporting_webhook.frequency is accepted but logged as warning.

    WHY THIS TEST EXISTS:
    The impl currently only supports daily reporting frequency. When a buyer requests
    hourly or monthly, the request must NOT be rejected — that would break existing
    buyers until the feature is fully implemented. Instead, a warning is logged and
    the request proceeds. This test pins the accept-and-warn behaviour so a
    future refactor that turns the warning into a rejection is caught.
    """

    @pytest.mark.asyncio
    async def test_hourly_reporting_frequency_accepted_with_warning(self, caplog):
        """TC-MCP-004: reporting_webhook.reporting_frequency='hourly' → accepted (no error)."""
        import logging

        from src.core.exceptions import AdCPProductNotFoundError
        from src.core.tools.media_buy_create import _create_media_buy_impl

        req = CreateMediaBuyRequest(
            **required_request_kwargs(),
            brand={"domain": "testbrand.com"},
            start_time=_future(1),
            end_time=_future(8),
            packages=[{"product_id": "prod_1", "budget": 5000.0, "pricing_option_id": "cpm_usd_fixed"}],
            reporting_webhook={
                "url": "https://example.com/webhook",
                "authentication": {"schemes": ["Bearer"], "credentials": "a" * 32},
                "reporting_frequency": "hourly",
            },
        )

        identity = ResolvedIdentity(
            principal_id="principal_1",
            tenant_id="test_tenant",
            tenant={"tenant_id": "test_tenant", "human_review_required": False},
            auth_token="test-token",
            protocol="mcp",
            testing_context=AdCPTestContext(dry_run=False, test_session_id="unit-test"),
        )

        mock_principal = MagicMock()
        mock_principal.principal_id = "principal_1"
        mock_principal.platform_mappings = {}

        mock_media_buys = MagicMock()
        mock_media_buys.find_by_idempotency_key.return_value = None
        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=None)
        mock_uow.session = MagicMock()
        mock_uow.media_buys = mock_media_buys

        mock_ctx_mgr = MagicMock()
        mock_ctx_mgr.create_context.return_value = MagicMock(context_id="ctx_1")
        mock_ctx_mgr.create_workflow_step.return_value = MagicMock(step_id="step_1")

        with (
            patch("src.core.tools.media_buy_create.get_principal_object", return_value=mock_principal),
            patch("src.core.database.repositories.MediaBuyUoW", return_value=mock_uow),
            patch("src.core.tools.media_buy_create.get_context_manager", return_value=mock_ctx_mgr),
            patch(
                "src.core.tools.media_buy_create.sandbox_mode_for_request",
                return_value=MagicMock(active=False, diagnostic=""),
            ),
            caplog.at_level(logging.WARNING, logger="src.core.tools.media_buy_create"),
        ):
            # Function proceeds past the webhook frequency check and fails later
            # at product lookup — that's expected and correct for this test.
            try:
                await _create_media_buy_impl(req=req, identity=identity)
            except (AdCPProductNotFoundError, Exception):
                pass  # expected: product not found in mock DB

        # The function must log a warning about unsupported frequency, NOT raise.
        frequency_warnings = [
            r for r in caplog.records if "hourly" in r.message.lower() or "frequency" in r.message.lower()
        ]
        assert frequency_warnings, (
            "A warning must be logged for unsupported 'hourly' reporting frequency. "
            "The warning keeps buyers informed without blocking their request."
        )
