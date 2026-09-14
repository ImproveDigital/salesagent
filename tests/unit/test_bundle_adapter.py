"""Unit tests for the bundle-inventory adapter registry (#521).

Exercises the registry + protocol + stub adapters. The GAM adapter's
SQL-backed methods are covered by integration tests in
``tests/integration/test_inventory_profiles_list_redesign.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.services.bundle_adapter import (
    BundleInventoryAdapter,
    BundleInventoryRow,
    adapter_for_tenant,
    get_adapter,
    iter_adapters,
)


class TestRegistry:
    """Registry resolution + tenant matching."""

    def test_four_adapters_registered_at_import(self):
        ids = {a.adapter_id for a in iter_adapters()}
        assert ids == {"gam", "improvedigital", "freewheel", "springserve"}

    def test_get_adapter_returns_registered_instance(self):
        gam = get_adapter("gam")
        assert gam is not None
        assert gam.adapter_id == "gam"
        assert gam.label == "Google Ad Manager"

    def test_get_adapter_unknown_returns_none(self):
        assert get_adapter("xandr") is None

    @pytest.mark.parametrize(
        "ad_server,expected",
        [
            ("google_ad_manager", "gam"),
            ("gam", "gam"),
            ("freewheel", "freewheel"),
            ("fw", "freewheel"),
            ("springserve", "springserve"),
            ("ss", "springserve"),
            ("improvedigital", "improvedigital"),
            ("improve_digital", "improvedigital"),
        ],
    )
    def test_adapter_for_tenant_matches_ad_server_aliases(self, ad_server, expected):
        adapter = adapter_for_tenant(ad_server)
        assert adapter is not None
        assert adapter.adapter_id == expected

    def test_adapter_for_tenant_unknown_returns_none(self):
        assert adapter_for_tenant("xandr") is None
        assert adapter_for_tenant(None) is None
        assert adapter_for_tenant("") is None


class TestRowShape:
    """``BundleInventoryRow`` is the wire format between adapters and UI."""

    def test_row_carries_external_id_name_entity_meta(self):
        row = BundleInventoryRow(
            external_id="14512330",
            name="Homepage Premium",
            entity_type="placement",
            meta="6 ad units · synced 3d ago",
        )
        assert row.external_id == "14512330"
        assert row.name == "Homepage Premium"
        assert row.entity_type == "placement"
        assert row.meta == "6 ad units · synced 3d ago"
        # raw defaults to empty so adapters that don't carry extras don't need to.
        assert row.raw == {}


class TestStubAdapters:
    """FreeWheel + SpringServe stubs return empty results but keep vocab."""

    def test_freewheel_stub_has_correct_label_and_vocab(self):
        fw = get_adapter("freewheel")
        assert fw is not None
        assert fw.label == "FreeWheel"
        # FW's "placements" is the primary leaf entity; "placement groups" the wrapper.
        assert fw.vocab == {"primary": "placements", "secondary": "placement groups"}

    def test_springserve_stub_has_correct_label_and_vocab(self):
        ss = get_adapter("springserve")
        assert ss is not None
        assert ss.label == "SpringServe"
        assert ss.vocab == {"primary": "tags", "secondary": "demand tags"}

    def test_stub_methods_return_empty_safely(self):
        """All read methods should return empty containers, not raise.

        Lets the blueprint code path that calls the adapter degrade
        cleanly for tenants on adapters whose sync surfaces aren't wired.
        """
        fw = get_adapter("freewheel")
        # session/tenant_id are unused by the stub but match the protocol.
        assert fw.has_synced_inventory(None, "t1") is False
        assert fw.count_inventory(None, "t1", "ad_unit") == 0
        assert fw.list_inventory_by_ids(None, "t1", "ad_unit", ["a", "b"]) == []
        assert fw.list_unbundled(None, "t1", {"ad_unit": set(), "placement": set()}, 10) == []
        assert fw.list_top_level_placements(None, "t1", 5) == []
        assert fw.find_inventory_item(None, "t1", "ad_unit", "missing") is None


class TestProtocolConformance:
    """Every registered adapter must satisfy the runtime-checked Protocol."""

    @pytest.mark.parametrize("adapter_id", ["gam", "improvedigital", "freewheel", "springserve"])
    def test_adapter_isinstance_protocol(self, adapter_id):
        adapter = get_adapter(adapter_id)
        assert isinstance(adapter, BundleInventoryAdapter)

    def test_only_improvedigital_pages_the_picker(self):
        paged = {a.adapter_id for a in iter_adapters() if a.picker_paged}
        assert paged == {"improvedigital"}


REPO_PATH = "src.core.database.repositories.improvedigital_inventory.ImproveDigitalInventoryRepository"


class TestImproveDigitalAdapter:
    """Improve Digital maps the bundle slots onto the 360Yield cache:
    bundle ``ad_unit`` → cached ``placement``, bundle ``placement`` →
    cached ``package``. Reads go through the picker projections so the
    editor never materializes ORM rows for the full placement set."""

    def _repo(self):
        repo = MagicMock()
        repo.list_picker_rows_by_ids.side_effect = lambda entity_type, ids: (
            [("7", "Pub Seven", None)]
            if entity_type == "publisher"
            else [(str(i), f"{entity_type}-{i}", "7") for i in ids]
        )
        return repo

    def test_label_and_vocab(self):
        adapter = get_adapter("improvedigital")
        assert adapter.label == "Improve Digital"
        assert adapter.vocab == {"primary": "placements", "secondary": "packages"}

    def test_search_maps_bundle_slots_to_cache_entity_types(self):
        adapter = get_adapter("improvedigital")
        repo = self._repo()
        repo.count_picker_rows.return_value = 3
        repo.list_picker_rows.return_value = [("11", "Home top", "7"), ("12", "Home side", "7")]
        with patch(REPO_PATH, return_value=repo):
            rows, total = adapter.search_inventory(None, "t1", "ad_unit", q="home", offset=0, limit=2)
        repo.count_picker_rows.assert_called_once_with("placement", q="home")
        repo.list_picker_rows.assert_called_once_with("placement", q="home", offset=0, limit=2)
        assert total == 3
        assert [r.external_id for r in rows] == ["11", "12"]
        assert all(r.entity_type == "ad_unit" for r in rows)
        # Placements are labelled with their publisher (one extra lookup, not per row).
        assert rows[0].meta == "Pub Seven"
        assert rows[0].raw["metadata"]["parent_id"] == "7"
        repo.list_picker_rows_by_ids.assert_called_once_with("publisher", ["7"])

    def test_bundle_placement_slot_reads_packages(self):
        adapter = get_adapter("improvedigital")
        repo = self._repo()
        repo.list_picker_rows.return_value = [("900", "Premium pack", None)]
        with patch(REPO_PATH, return_value=repo):
            rows = adapter.list_inventory(None, "t1", "placement", limit=5)
        repo.list_picker_rows.assert_called_once_with("package", limit=5)
        assert rows[0].entity_type == "placement"
        assert rows[0].name == "Premium pack"

    def test_coverage_counts_only_directly_picked_placements(self):
        """Package membership is a live lookup, not cached — packages must
        not inflate coverage."""
        adapter = get_adapter("improvedigital")
        repo = self._repo()
        with patch(REPO_PATH, return_value=repo):
            covered = adapter.coverage_for_bundle(None, "t1", {"ad_units": ["1", "2"], "placements": ["900"]})
        assert covered == 2
        repo.list_picker_rows_by_ids.assert_called_once_with("placement", ["1", "2"])

    def test_unbundled_excludes_bundled_ids_and_lists_packages_first(self):
        adapter = get_adapter("improvedigital")
        repo = self._repo()
        repo.list_picker_rows.side_effect = lambda entity_type, **kw: (
            [("900", "Pack", None)] if entity_type == "package" else [("11", "Home top", "7")]
        )
        with patch(REPO_PATH, return_value=repo):
            rows = adapter.list_unbundled(None, "t1", {"ad_unit": {"12"}, "placement": {"901"}}, limit=10)
        assert [(r.entity_type, r.external_id) for r in rows] == [("placement", "900"), ("ad_unit", "11")]
        calls = {c.args[0]: c.kwargs for c in repo.list_picker_rows.call_args_list}
        assert set(calls["package"]["exclude_ids"]) == {"901"}
        assert set(calls["placement"]["exclude_ids"]) == {"12"}

    def test_unknown_entity_type_is_rejected(self):
        adapter = get_adapter("improvedigital")
        with patch(REPO_PATH, return_value=self._repo()), pytest.raises(ValueError):
            adapter.count_inventory(None, "t1", "size")
