"""Tests for the shared sync/delivery/permissions helpers on AdServerAdapter.

Covers the base-layer framework used by pull-based (reporting-cache) ad
server adapters: AdapterSyncResult, DeliveryDataUnavailable, the
run_inventory_sync/run_reporting_sync hooks, permission probing, and the
delivery/pricing response helpers.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from src.adapters.base import (
    AdapterSyncResult,
    AdServerAdapter,
    DeliveryDataUnavailable,
    PermissionsReport,
)
from src.adapters.constants import REQUIRED_UPDATE_ACTIONS
from src.core.exceptions import AdCPAdapterError, AdCPCapabilityNotSupportedError, AdCPValidationError
from src.core.schemas import (
    FormatId,
    MediaPackage,
    Principal,
    ReportingPeriod,
    Targeting,
)

pytestmark = pytest.mark.unit

DEFAULT_AGENT_URL = "https://creative.adcontextprotocol.org"


class _StubAdapter(AdServerAdapter):
    """Minimal concrete adapter to exercise base-class helpers."""

    adapter_name = "stub"

    def create_media_buy(self, request, packages, start_time, end_time, package_pricing_info=None):
        raise NotImplementedError

    def add_creative_assets(self, media_buy_id, assets, today):
        raise NotImplementedError

    def associate_creatives(self, line_item_ids, platform_creative_ids):
        raise NotImplementedError

    def check_media_buy_status(self, media_buy_id, today):
        raise NotImplementedError

    def get_media_buy_delivery(self, media_buy_id, date_range, today):
        raise NotImplementedError

    def update_media_buy_performance_index(self, media_buy_id, package_performance):
        raise NotImplementedError

    def update_media_buy(self, media_buy_id, action, package_id, budget, today):
        raise NotImplementedError


@pytest.fixture
def adapter() -> _StubAdapter:
    principal = Principal(
        principal_id="test_principal",
        name="Test Principal",
        platform_mappings={"mock": {"advertiser_id": "adv_1"}},
    )
    return _StubAdapter({}, principal, tenant_id="test_tenant")


def _make_package(package_id: str = "pkg_1", **overrides: Any) -> MediaPackage:
    kwargs: dict[str, Any] = {
        "package_id": package_id,
        "name": "Test Package",
        "delivery_type": "guaranteed",
        "cpm": 10.0,
        "impressions": 100000,
        "format_ids": [FormatId(agent_url=DEFAULT_AGENT_URL, id="display_300x250")],
    }
    kwargs.update(overrides)
    return MediaPackage(**kwargs)


def _period(days: int = 14) -> ReportingPeriod:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    return ReportingPeriod(start=start, end=start + timedelta(days=days))


class TestAdapterSyncResult:
    def test_total_count_sums_all_counts(self):
        result = AdapterSyncResult(
            sync_kind="inventory",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            succeeded=True,
            counts={"placement": 3, "size": 2},
        )
        assert result.total_count == 5

    def test_total_count_empty_is_zero(self):
        result = AdapterSyncResult(
            sync_kind="reporting",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            succeeded=False,
        )
        assert result.total_count == 0


class TestDeliveryDataUnavailable:
    def test_message_without_reason(self):
        exc = DeliveryDataUnavailable("mb_123")
        assert str(exc) == "Delivery data not yet available for mb_123"
        assert exc.media_buy_id == "mb_123"
        assert exc.reason is None

    def test_message_with_reason(self):
        exc = DeliveryDataUnavailable("mb_123", reason="reporting sync has not run")
        assert str(exc) == "Delivery data not yet available for mb_123: reporting sync has not run"
        assert exc.reason == "reporting sync has not run"


class TestSyncHookDefaults:
    def test_run_inventory_sync_default_raises_not_implemented(self, adapter):
        with pytest.raises(NotImplementedError, match="run_inventory_sync"):
            adapter.run_inventory_sync()

    def test_run_reporting_sync_default_raises_not_implemented(self, adapter):
        with pytest.raises(NotImplementedError, match="run_reporting_sync"):
            adapter.run_reporting_sync()

    def test_latest_sync_timestamps_default_to_none(self, adapter):
        assert adapter.latest_inventory_sync_at() is None
        assert adapter.latest_reporting_sync_at() is None

    def test_supports_reporting_sync_defaults_false(self, adapter):
        assert adapter.capabilities.supports_reporting_sync is False


class TestRaiseUnsupportedAction:
    def test_raises_typed_error_with_canonical_action_list(self):
        with pytest.raises(AdCPCapabilityNotSupportedError) as exc_info:
            AdServerAdapter._raise_unsupported_action("explode_order")
        message = str(exc_info.value)
        assert "explode_order" in message
        for action in REQUIRED_UPDATE_ACTIONS:
            assert action in message


class TestSimulatedDeliveryResponse:
    def test_mid_flight_progress_scales_impressions_and_spend(self, adapter):
        period = _period(days=14)
        today = period.start + timedelta(days=7)
        response = adapter._simulated_delivery_response(
            "mb_1", period, today, target_impressions=100000, cpm=10.0, flight_days=14
        )
        # 7/14 elapsed * 100000 * 0.95 = 47500 impressions, spend = 47500 * 10 / 1000
        assert response.totals.impressions == 47500
        assert response.totals.spend == pytest.approx(475.0)
        assert response.media_buy_id == "mb_1"
        assert response.currency == "USD"

    def test_progress_caps_at_full_flight(self, adapter):
        period = _period(days=14)
        today = period.start + timedelta(days=60)
        response = adapter._simulated_delivery_response(
            "mb_1", period, today, target_impressions=100000, cpm=10.0, flight_days=14
        )
        assert response.totals.impressions == 95000

    def test_completion_rate_derives_completed_views(self, adapter):
        period = _period(days=14)
        today = period.start + timedelta(days=14)
        response = adapter._simulated_delivery_response(
            "mb_1", period, today, target_impressions=100000, cpm=10.0, completion_rate=0.5, flight_days=14
        )
        assert response.totals.completed_views == int(95000 * 0.5)


class TestAggregateStatRows:
    def test_aggregates_totals_and_per_package_rows(self):
        rows = [
            SimpleNamespace(
                line_item_id="li_1", impressions=1000, spend_micros=5_000_000, completed_views=100, currency="EUR"
            ),
            SimpleNamespace(
                line_item_id="li_2", impressions=3000, spend_micros=15_000_000, completed_views=None, currency=None
            ),
        ]
        response = AdServerAdapter._aggregate_stat_rows_to_delivery_response(
            "mb_1", _period(), rows, package_id_attr="line_item_id"
        )
        assert response.totals.impressions == 4000
        assert response.totals.spend == pytest.approx(20.0)
        assert response.totals.completed_views == 100
        assert response.totals.completion_rate == pytest.approx(100 / 4000)
        assert response.currency == "EUR"
        assert [p.package_id for p in response.by_package] == ["li_1", "li_2"]
        assert response.by_package[0].spend == pytest.approx(5.0)
        assert response.by_package[1].completed_views is None

    def test_empty_rows_fall_back_to_default_currency(self):
        response = AdServerAdapter._aggregate_stat_rows_to_delivery_response(
            "mb_1", _period(), [], package_id_attr="line_item_id", default_currency="GBP"
        )
        assert response.totals.impressions == 0
        assert response.totals.completion_rate is None
        assert response.currency == "GBP"
        assert response.by_package == []


class TestResolvePricingRate:
    def test_fixed_rate_resolves(self):
        package = _make_package()
        rate, rate_type = AdServerAdapter._resolve_pricing_rate(
            package, {"pkg_1": {"rate": 12.5, "is_fixed": True, "pricing_model": "cpm"}}
        )
        assert rate == 12.5
        assert rate_type == "CPM"

    def test_auction_uses_bid_price(self):
        package = _make_package()
        rate, _ = AdServerAdapter._resolve_pricing_rate(
            package, {"pkg_1": {"rate": 12.5, "is_fixed": False, "bid_price": 8.0, "pricing_model": "cpm"}}
        )
        assert rate == 8.0

    def test_flat_rate_pricing_model_maps_rate_type(self):
        package = _make_package()
        _, rate_type = AdServerAdapter._resolve_pricing_rate(
            package, {"pkg_1": {"rate": 500.0, "is_fixed": True, "pricing_model": "flat_rate"}}
        )
        assert rate_type == "FLAT_RATE"

    def test_missing_pricing_info_raises_adapter_error(self):
        package = _make_package()
        with pytest.raises(AdCPAdapterError, match="Missing pricing info"):
            AdServerAdapter._resolve_pricing_rate(package, None)

    def test_auction_without_bid_price_raises_validation_error(self):
        package = _make_package()
        with pytest.raises(AdCPValidationError, match="no bid_price"):
            AdServerAdapter._resolve_pricing_rate(package, {"pkg_1": {"rate": 12.5, "is_fixed": False}})


class TestValidateTargetingOrRaise:
    def test_no_overlay_passes(self):
        packages = [_make_package()]
        assert AdServerAdapter._validate_targeting_or_raise(packages, lambda t: ["nope"], adapter_name="stub") is None

    def test_clean_overlay_passes(self):
        packages = [_make_package(targeting_overlay=Targeting())]
        assert AdServerAdapter._validate_targeting_or_raise(packages, lambda t: [], adapter_name="stub") is None

    def test_unsupported_targeting_raises_typed_error(self):
        packages = [_make_package(targeting_overlay=Targeting())]
        with pytest.raises(AdCPCapabilityNotSupportedError) as exc_info:
            AdServerAdapter._validate_targeting_or_raise(
                packages, lambda t: ["postal areas not supported"], adapter_name="stub"
            )
        message = str(exc_info.value)
        assert "postal areas not supported" in message
        assert "stub" in message


class TestCheckPermissions:
    def test_default_report_is_fully_operational_with_no_checks(self, adapter):
        report = adapter.check_permissions()
        assert isinstance(report, PermissionsReport)
        assert report.adapter == "stub"
        assert report.tenant_id == "test_tenant"
        assert report.fully_operational is True
        assert report.checks == []

    def test_new_permissions_report_scaffold(self, adapter):
        report = adapter._new_permissions_report()
        assert report.fully_operational is False
        assert report.checks == []
        assert report.error is None

    def test_new_permissions_report_dry_run_message(self):
        principal = Principal(
            principal_id="test_principal",
            name="Test Principal",
            platform_mappings={"mock": {"advertiser_id": "adv_1"}},
        )
        dry = _StubAdapter({}, principal, dry_run=True, tenant_id="test_tenant")
        report = dry._new_permissions_report(dry_run_message="dry-run: no probes")
        assert report.error == "dry-run: no probes"

    def test_walk_probes_grants_on_non_auth_status(self, adapter):
        report = adapter._new_permissions_report()
        probes = [
            ("read_a", "Read A", "GET", "/a?limit=1", True, "feature_a"),
            ("read_b", "Read B", "GET", "/b", True, "feature_b"),
            ("read_c", "Read C", "GET", "/c", False, "feature_c"),
        ]
        statuses = {"/a?limit=1": (200, ""), "/b": (403, "forbidden body"), "/c": (404, "")}

        adapter._walk_permission_probes(report, probes, lambda method, path: statuses[path])

        granted = {c.name: c.granted for c in report.checks}
        # 200 and 404 count as granted (endpoint accepted the call); 403 does not.
        assert granted == {"read_a": True, "read_b": False, "read_c": True}
        assert report.checks[1].detail == "403: forbidden body"
        assert report.checks[0].probe_target == "GET /a"
        assert report.fully_operational is False  # required read_b denied

    def test_walk_probes_rollup_ignores_optional_denials(self, adapter):
        report = adapter._new_permissions_report()
        probes = [
            ("read_a", "Read A", "GET", "/a", True, "feature_a"),
            ("read_c", "Read C", "GET", "/c", False, "feature_c"),
        ]
        statuses = {"/a": (200, ""), "/c": (401, "")}

        adapter._walk_permission_probes(report, probes, lambda method, path: statuses[path])

        assert report.fully_operational is True

    def test_walk_probes_auth_error_stops_early(self, adapter):
        report = adapter._new_permissions_report()
        probes = [
            ("read_a", "Read A", "GET", "/a", True, "feature_a"),
            ("read_b", "Read B", "GET", "/b", True, "feature_b"),
        ]

        def probe_fn(method: str, path: str) -> tuple[int, str]:
            raise RuntimeError("bad credentials")

        adapter._walk_permission_probes(report, probes, probe_fn, auth_error_types=(RuntimeError,))

        assert report.error == "Authentication failed: bad credentials"
        assert report.checks == []
        assert report.fully_operational is False
