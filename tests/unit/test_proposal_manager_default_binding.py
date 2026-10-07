"""Tenants created after boot still resolve the shared proposal manager."""

from __future__ import annotations

from core.main import _DefaultingProposalManagers


def test_known_tenant_returns_its_manager():
    bound, shared = object(), object()
    managers = _DefaultingProposalManagers({"t1": bound}, shared)
    assert managers.get("t1") is bound
    assert managers["t1"] is bound


def test_unknown_tenant_falls_back_to_shared_manager():
    shared = object()
    managers = _DefaultingProposalManagers({}, shared)
    assert managers.get("created-after-boot") is shared
    # explicit callers can still supply their own fallback
    sentinel = object()
    assert managers.get("created-after-boot", sentinel) is sentinel


def test_behaves_like_a_dict_for_iteration_and_membership():
    shared = object()
    managers = _DefaultingProposalManagers({"t1": shared}, shared)
    assert isinstance(managers, dict)
    assert list(managers.values()) == [shared]
    assert "t2" not in managers
