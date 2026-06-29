"""Unit tests for _detect_overbook_warnings — GAM inventory availability pre-flight check.

The function compares the implied impression goal (budget / CPM * 1000) against
GAM's availability forecast for the product's ad units. It is a fail-open,
informational-only check: it never blocks the buy, it only populates ext.warnings.

Guard conditions that cause immediate return of [] (no warning):
  TC-GAMW-004 — non-GAM adapter
  TC-GAMW-003 — multiple products in the buy
  TC-GAMW-007 — zero or negative total_budget
  TC-GAMW-006 — non-CPM pricing model (no meaningful CPM to derive impressions from)

Forecast-dependent results:
  TC-GAMW-005 — forecast API call fails → fail-open ([] returned, buy proceeds)
  TC-GAMW-001 — implied impressions <= forecast → no warning
  TC-GAMW-002 — implied impressions > forecast → warning with overbook details in ext

WHY THESE TESTS EXIST:
_detect_overbook_warnings is the only consumer of the GAMForecastManager in the
create-media-buy path. Without these tests, any change to the guard conditions
(e.g. accidentally removing the multi-product guard) would silently run expensive
GAM API calls for every buy, or add spurious warnings to every response.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Adapter stub helpers
# ---------------------------------------------------------------------------


def _gam_adapter(
    *,
    ad_unit_ids: list[str] | None = None,
    orders_manager: MagicMock | None = None,
) -> object:
    """Adapter stub whose __class__.__name__ == 'GoogleAdManager'.

    The overbook check uses  `adapter.__class__.__name__ != "GoogleAdManager"` as its
    first guard. A named class is required because MagicMock's class name doesn't
    match the string literal the code checks against.
    """
    class GoogleAdManager:
        pass

    adapter = GoogleAdManager()
    adapter.orders_manager = orders_manager or MagicMock()
    return adapter


def _non_gam_adapter() -> object:
    """Adapter stub with a class name that is NOT 'GoogleAdManager'."""
    class MockAdapter:
        pass

    return MockAdapter()


def _cpm_product(
    product_id: str = "prod_display",
    rate: float = 5.0,
    pricing_model: str = "cpm",
) -> MagicMock:
    """Mock Product with a single CPM pricing option."""
    pricing_option = MagicMock()
    pricing_option.pricing_model = pricing_model
    pricing_option.rate = rate
    # Simulate RootModel unwrap (adcp 2.14.0+): getattr(po, "root", po) -> po itself.
    pricing_option.root = pricing_option

    product = MagicMock()
    product.product_id = product_id
    product.pricing_options = [pricing_option]
    return product


def _mock_request(start_days: int = 1, end_days: int = 8) -> MagicMock:
    """Minimal mock request with flight dates for the overbook calculation."""
    now = datetime.now(UTC)
    req = MagicMock()
    req.flight_start_date = None  # force fallback to start_time
    req.flight_end_date = None
    req.start_time = now + timedelta(days=start_days)
    req.end_time = now + timedelta(days=end_days)
    return req


_PATCH_FORECAST_MGR = "src.adapters.gam.managers.forecast.GAMForecastManager"


# ===========================================================================
# Guard cases — all return [] without touching the forecast API
# ===========================================================================


class TestOverbookGuards:

    def test_non_gam_adapter_skips_check(self):
        """TC-GAMW-004: adapter is not GoogleAdManager → [] without any forecast call.

        WHY THIS TEST EXISTS:
        The overbook check is GAM-specific. Running it against Mock or SpringServe
        adapters would crash (they have no orders_manager) or return meaningless data.
        The first guard must short-circuit on non-GAM adapters.
        """
        from src.core.tools.media_buy_create import _detect_overbook_warnings

        result = _detect_overbook_warnings(
            adapter=_non_gam_adapter(),
            products_in_buy=[_cpm_product()],
            effective_configs={"prod_display": {"targeted_ad_unit_ids": ["au_1"]}},
            req=_mock_request(),
            total_budget=5000.0,
        )

        assert result == [], "Non-GAM adapter must return no warnings without API calls."

    def test_multiple_products_skips_check(self):
        """TC-GAMW-003: buy has 2+ products → [] (multi-product budget allocation not implemented).

        WHY THIS TEST EXISTS:
        Budget allocation across multiple products requires knowing each product's CPM
        share, which is non-trivial. The design decision (per #152) is to skip the
        overbook check for multi-product buys rather than guess. Removing this guard
        would compute an inflated impression goal using the combined budget, leading to
        false overbook warnings on every multi-product buy.
        """
        from src.core.tools.media_buy_create import _detect_overbook_warnings

        result = _detect_overbook_warnings(
            adapter=_gam_adapter(),
            products_in_buy=[_cpm_product("prod_1"), _cpm_product("prod_2")],
            effective_configs={},
            req=_mock_request(),
            total_budget=5000.0,
        )

        assert result == [], "Multi-product buy must return no warnings."

    def test_zero_budget_skips_check(self):
        """TC-GAMW-007: total_budget = 0 → [] (cannot derive impressions from zero budget).

        WHY THIS TEST EXISTS:
        Implied impressions = budget / CPM * 1000. With budget=0 the result is 0,
        which would never overbook. The guard avoids the division and any API calls.
        Also catches sandbox buys with zero economics.
        """
        from src.core.tools.media_buy_create import _detect_overbook_warnings

        result = _detect_overbook_warnings(
            adapter=_gam_adapter(),
            products_in_buy=[_cpm_product()],
            effective_configs={"prod_display": {"targeted_ad_unit_ids": ["au_1"]}},
            req=_mock_request(),
            total_budget=0.0,
        )

        assert result == [], "Zero budget must return no warnings."

    def test_non_cpm_pricing_skips_check(self):
        """TC-GAMW-006: product pricing model is not CPM → [] (no meaningful impression derivation).

        WHY THIS TEST EXISTS:
        CPD (cost-per-day) or flat-rate pricing cannot be divided by CPM to derive
        an impression goal. Running the check for these models would require
        model-specific formulas. The guard skips to avoid incorrect overbook warnings
        on non-CPM products.
        """
        from src.core.tools.media_buy_create import _detect_overbook_warnings

        cpd_product = _cpm_product(pricing_model="cpd")  # not "cpm"

        result = _detect_overbook_warnings(
            adapter=_gam_adapter(),
            products_in_buy=[cpd_product],
            effective_configs={"prod_display": {"targeted_ad_unit_ids": ["au_1"]}},
            req=_mock_request(),
            total_budget=5000.0,
        )

        assert result == [], "Non-CPM pricing must return no warnings."

    def test_no_ad_unit_ids_in_config_skips_check(self):
        """TC-GAMW extra: no targeted_ad_unit_ids in implementation config → [].

        WHY THIS TEST EXISTS:
        The forecast API requires specific GAM ad unit IDs. If none are configured
        for the product, the function has nothing to query. The guard prevents an
        empty GAM availability request which would either error or return misleading data.
        """
        from src.core.tools.media_buy_create import _detect_overbook_warnings

        result = _detect_overbook_warnings(
            adapter=_gam_adapter(),
            products_in_buy=[_cpm_product()],
            effective_configs={"prod_display": {}},  # no targeted_ad_unit_ids
            req=_mock_request(),
            total_budget=5000.0,
        )

        assert result == [], "Missing ad_unit_ids config must return no warnings."


# ===========================================================================
# Forecast-dependent cases
# ===========================================================================


class TestOverbookForecast:
    """Tests that exercise the GAM forecast API call path."""

    def _gam_adapter_with_client(self):
        mock_orders_mgr = MagicMock()
        mock_orders_mgr.client_manager = MagicMock()
        mock_orders_mgr.advertiser_id = "adv_1"
        return _gam_adapter(orders_manager=mock_orders_mgr)

    def _effective_configs_with_units(self, product_id: str = "prod_display") -> dict:
        return {product_id: {"targeted_ad_unit_ids": ["au_1", "au_2"]}}

    def test_forecast_api_failure_returns_empty_fail_open(self):
        """TC-GAMW-005: GAM availability API returns None → [] (fail-open, buy proceeds).

        WHY THIS TEST EXISTS:
        The forecast call goes to GAM's external API. Network failures, GAM errors,
        or unavailable ad units must never block a buy. The function is informational
        only. Returning None from get_available_units triggers the fail-open path.
        If this guard is removed, a GAM API outage would prevent all new buys from
        getting overbook warnings — and because the check is async, the missing check
        would be invisible without an explicit test.
        """
        from src.core.tools.media_buy_create import _detect_overbook_warnings

        adapter = self._gam_adapter_with_client()
        product = _cpm_product()  # CPM, rate=5.0

        with patch(_PATCH_FORECAST_MGR) as MockForecastMgr:
            MockForecastMgr.return_value.get_available_units.return_value = None

            result = _detect_overbook_warnings(
                adapter=adapter,
                products_in_buy=[product],
                effective_configs=self._effective_configs_with_units(),
                req=_mock_request(),
                total_budget=5000.0,
            )

        assert result == [], "Forecast API failure must be fail-open: return [], not raise."

    def test_impressions_within_forecast_returns_no_warning(self):
        """TC-GAMW-001: implied impressions <= GAM availability → [] (no overbook warning).

        WHY THIS TEST EXISTS:
        The common case: the buyer's budget fits within what GAM has available.
        No warning should appear in ext.warnings. If the comparison operator is wrong
        (e.g. >= instead of >) this test catches it.

        Math: budget=1000, CPM=5.0 → implied=200,000; forecast=500,000 → no overbook.
        """
        from src.core.tools.media_buy_create import _detect_overbook_warnings

        adapter = self._gam_adapter_with_client()
        product = _cpm_product(rate=5.0)

        with patch(_PATCH_FORECAST_MGR) as MockForecastMgr:
            MockForecastMgr.return_value.get_available_units.return_value = 500_000

            result = _detect_overbook_warnings(
                adapter=adapter,
                products_in_buy=[product],
                effective_configs=self._effective_configs_with_units(),
                req=_mock_request(),
                total_budget=1000.0,  # 1000/5 * 1000 = 200,000 impressions
            )

        assert result == [], "Impressions within forecast must not trigger a warning."

    def test_impressions_exceeding_forecast_returns_overbook_warning(self):
        """TC-GAMW-002: implied impressions > GAM availability → ext.warnings entry.

        WHY THIS TEST EXISTS:
        When the buyer's budget implies more impressions than GAM has available,
        the line item may land in INVENTORY_RELEASED. The warning must appear in the
        response ext so buyers can adjust budget or targeting before the buy under-paces.
        This test pins the warning code, message content, and details structure.

        Math: budget=5000, CPM=5.0 → implied=1,000,000; forecast=500,000 → 100% overbook.
        """
        from src.core.tools.media_buy_create import _detect_overbook_warnings

        adapter = self._gam_adapter_with_client()
        product = _cpm_product(rate=5.0)

        with patch(_PATCH_FORECAST_MGR) as MockForecastMgr:
            MockForecastMgr.return_value.get_available_units.return_value = 500_000

            result = _detect_overbook_warnings(
                adapter=adapter,
                products_in_buy=[product],
                effective_configs=self._effective_configs_with_units(),
                req=_mock_request(),
                total_budget=5000.0,  # 5000/5 * 1000 = 1,000,000 → exceeds 500k
            )

        assert len(result) == 1, "One overbook warning must be returned."
        warning = result[0]
        assert warning["code"] == "inventory_overbook_minor", (
            "Warning code must be 'inventory_overbook_minor' for buyers to identify it programmatically."
        )
        assert "goal_impressions" in warning.get("details", {}), (
            "Warning details must include goal_impressions for buyer diagnosis."
        )
        assert "forecast_available_impressions" in warning.get("details", {}), (
            "Warning details must include the GAM forecast value."
        )
        assert "overbook_percent" in warning.get("details", {}), (
            "Warning details must include overbook_percent so buyers know the severity."
        )
