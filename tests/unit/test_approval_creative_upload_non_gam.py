"""Regression tests: approval-path creative upload for adapters without a
GAM-style ``creatives_manager`` (e.g. Improve Digital).

``execute_approved_media_buy`` previously uploaded creatives only through
``adapter.creatives_manager`` — a GAM-only attribute — so buys approved from
the workflow queue on non-GAM adapters reached the platform with no
creatives ("[APPROVAL] Adapter does not support creative upload, skipping").
The upload must fall back to ``adapter.add_creative_assets`` and bind the
returned platform creative IDs to the packages' platform line items.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.core.tools.media_buy_create import _upload_approval_creatives_via_adapter


def _asset(creative_id: str, package_id: str) -> dict:
    return {
        "creative_id": creative_id,
        "package_assignments": [{"package_id": package_id, "weight": 100}],
        "width": 300,
        "height": 250,
        "url": "https://cdn.example.com/banner.jpg",
        "click_url": "https://example.com",
        "asset_type": "image",
        "name": creative_id,
    }


def _adapter(statuses: list) -> MagicMock:
    adapter = MagicMock(spec=["add_creative_assets", "associate_creatives"])
    adapter.add_creative_assets.return_value = statuses
    return adapter


class TestApprovalCreativeUploadNonGam:
    def test_uploads_persists_platform_id_and_associates_line_items(self):
        adapter = _adapter([SimpleNamespace(creative_id="991", status="approved", message=None)])
        creative = SimpleNamespace(data={})
        session = MagicMock()

        _upload_approval_creatives_via_adapter(
            adapter=adapter,
            platform_media_buy_id="improvedigital_314399",
            assets=[_asset("cr_1", "pkg_1")],
            creative_map={"cr_1": creative},
            platform_line_item_ids={"pkg_1": "5501"},
            session=session,
        )

        args = adapter.add_creative_assets.call_args[0]
        assert args[0] == "improvedigital_314399"
        assert args[1][0]["creative_id"] == "cr_1"
        assert creative.data["platform_creative_id"] == "991"
        session.add.assert_called_once_with(creative)
        adapter.associate_creatives.assert_called_once_with(["5501"], ["991"])

    def test_failed_upload_is_skipped_not_persisted(self):
        adapter = _adapter([SimpleNamespace(creative_id="cr_1", status="failed", message="rejected")])
        creative = SimpleNamespace(data={})
        session = MagicMock()

        _upload_approval_creatives_via_adapter(
            adapter=adapter,
            platform_media_buy_id="improvedigital_314399",
            assets=[_asset("cr_1", "pkg_1")],
            creative_map={"cr_1": creative},
            platform_line_item_ids={"pkg_1": "5501"},
            session=session,
        )

        assert "platform_creative_id" not in creative.data
        session.add.assert_not_called()
        adapter.associate_creatives.assert_not_called()

    def test_existing_platform_creative_id_is_preserved(self):
        adapter = _adapter([SimpleNamespace(creative_id="991", status="approved", message=None)])
        creative = SimpleNamespace(data={"platform_creative_id": "888"})
        session = MagicMock()

        _upload_approval_creatives_via_adapter(
            adapter=adapter,
            platform_media_buy_id="improvedigital_314399",
            assets=[_asset("cr_1", "pkg_1")],
            creative_map={"cr_1": creative},
            platform_line_item_ids={"pkg_1": "5501"},
            session=session,
        )

        assert creative.data["platform_creative_id"] == "888"
        session.add.assert_not_called()
        # Association still uses the freshly uploaded platform ID
        adapter.associate_creatives.assert_called_once_with(["5501"], ["991"])

    def test_creative_shared_across_packages_binds_each_line_item(self):
        adapter = _adapter([SimpleNamespace(creative_id="991", status="approved", message=None)])
        asset = _asset("cr_1", "pkg_1")
        asset["package_assignments"].append({"package_id": "pkg_2", "weight": 100})
        session = MagicMock()

        _upload_approval_creatives_via_adapter(
            adapter=adapter,
            platform_media_buy_id="improvedigital_314399",
            assets=[asset],
            creative_map={"cr_1": SimpleNamespace(data={})},
            platform_line_item_ids={"pkg_1": "5501", "pkg_2": "5502"},
            session=session,
        )

        assert adapter.associate_creatives.call_count == 2
        called = {call.args[0][0]: call.args[1] for call in adapter.associate_creatives.call_args_list}
        assert called == {"5501": ["991"], "5502": ["991"]}

    def test_adapter_without_associate_creatives_still_uploads(self):
        adapter = MagicMock(spec=["add_creative_assets"])
        adapter.add_creative_assets.return_value = [SimpleNamespace(creative_id="991", status="approved", message=None)]
        creative = SimpleNamespace(data={})
        session = MagicMock()

        _upload_approval_creatives_via_adapter(
            adapter=adapter,
            platform_media_buy_id="improvedigital_314399",
            assets=[_asset("cr_1", "pkg_1")],
            creative_map={"cr_1": creative},
            platform_line_item_ids={"pkg_1": "5501"},
            session=session,
        )

        assert creative.data["platform_creative_id"] == "991"
