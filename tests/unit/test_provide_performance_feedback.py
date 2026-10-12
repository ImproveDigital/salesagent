"""Unit tests for _provide_performance_feedback_impl (AdCP provide_performance_feedback).

The tool previously returned ``success: true`` from a stub without looking
at the request, so unknown media buys were silently "accepted".
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from src.core.exceptions import (
    AdCPAdapterError,
    AdCPInvalidStateError,
    AdCPMediaBuyNotFoundError,
    AdCPPackageNotFoundError,
    AdCPValidationError,
)
from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import PackagePerformance
from src.core.tools.performance import _provide_performance_feedback_impl

_MOD = "src.core.tools.performance"
_PERIOD = {"start": "2026-09-23T00:00:00Z", "end": "2026-09-28T00:00:00Z"}


def _identity() -> ResolvedIdentity:
    return ResolvedIdentity(
        principal_id="principal_1",
        tenant_id="tenant_1",
        tenant={"tenant_id": "tenant_1"},
        protocol="mcp",
    )


def _patched(
    stack: ExitStack,
    *,
    package_ids: list[str] | None = None,
    adapter_ok: bool = True,
    unmaterialized: set[str] | None = None,
    verify_error: Exception | None = None,
) -> dict[str, MagicMock]:
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.__exit__.return_value = False
    packages = []
    for pid in package_ids if package_ids is not None else ["pkg_1", "pkg_2"]:
        pkg = MagicMock()
        pkg.package_id = pid
        packages.append(pkg)
    uow.media_buys.get_packages.return_value = packages

    adapter = MagicMock()
    adapter.update_media_buy_performance_index.return_value = adapter_ok
    audit = MagicMock()

    stack.enter_context(patch(f"{_MOD}.MediaBuyUoW", return_value=uow))
    verify = stack.enter_context(patch(f"{_MOD}._verify_principal", side_effect=verify_error))
    stack.enter_context(patch(f"{_MOD}.unmaterialized_projected_ids", return_value=unmaterialized or set()))
    stack.enter_context(patch(f"{_MOD}.get_principal_object", return_value=MagicMock()))
    stack.enter_context(patch(f"{_MOD}.get_adapter", return_value=adapter))
    stack.enter_context(patch(f"{_MOD}.get_audit_logger", return_value=audit))
    return {"adapter": adapter, "audit": audit, "verify": verify}


def _call(**overrides):
    kwargs = {
        "media_buy_id": "mb_1",
        "performance_index": 1.2,
        "measurement_period": _PERIOD,
        "identity": _identity(),
    }
    kwargs.update(overrides)
    return _provide_performance_feedback_impl(**kwargs)


def test_package_feedback_forwarded_to_adapter_and_audited():
    with ExitStack() as stack:
        m = _patched(stack)
        assert _call(package_id="pkg_2", metric_type="overall_performance") is True

    m["adapter"].update_media_buy_performance_index.assert_called_once_with(
        "mb_1", [PackagePerformance(package_id="pkg_2", performance_index=1.2)]
    )
    details = m["audit"].log_operation.call_args.kwargs["details"]
    assert details["media_buy_id"] == "mb_1"
    assert details["package_id"] == "pkg_2"
    assert details["performance_index"] == 1.2
    assert details["metric_type"] == "overall_performance"


def test_buy_level_feedback_applies_to_every_package():
    with ExitStack() as stack:
        m = _patched(stack, package_ids=["pkg_1", "pkg_2"])
        _call()

    _, perf = m["adapter"].update_media_buy_performance_index.call_args.args
    assert [p.package_id for p in perf] == ["pkg_1", "pkg_2"]


def test_unknown_media_buy_is_rejected():
    with ExitStack() as stack:
        m = _patched(stack, verify_error=AdCPMediaBuyNotFoundError("Media buy 'nope' not found."))
        with pytest.raises(AdCPMediaBuyNotFoundError):
            _call(media_buy_id="nope")
    m["adapter"].update_media_buy_performance_index.assert_not_called()


def test_unknown_package_is_rejected():
    with ExitStack() as stack:
        m = _patched(stack, package_ids=["pkg_1"])
        with pytest.raises(AdCPPackageNotFoundError):
            _call(package_id="pkg_other")
    m["adapter"].update_media_buy_performance_index.assert_not_called()


def test_unclaimed_imported_gam_order_is_invalid_state():
    with ExitStack() as stack:
        m = _patched(stack, unmaterialized={"gam_123"})
        with pytest.raises(AdCPInvalidStateError, match="update_media_buy"):
            _call(media_buy_id="gam_123")
    m["verify"].assert_not_called()


def test_inverted_measurement_period_is_rejected():
    with ExitStack() as stack:
        _patched(stack)
        with pytest.raises(AdCPValidationError):
            _call(measurement_period={"start": _PERIOD["end"], "end": _PERIOD["start"]})


def test_adapter_rejection_surfaces_as_adapter_error():
    with ExitStack() as stack:
        _patched(stack, adapter_ok=False)
        with pytest.raises(AdCPAdapterError):
            _call()
