"""Regression tests: creative gates must run on every booking/assignment path (FMS-2162).

Two gaps let a wrong creative reach a live buy:

- ``update_media_buy`` only checked creative formats on the apply path, which
  sits after the manual-approval gate, so a mismatched assignment was accepted
  as a deferred ``requires_approval``.
- ``create_media_buy`` only required a click-through URL (Improve Digital) when
  a buy was booked after manual approval; a buy booked immediately skipped it.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.core.exceptions import AdCPValidationError
from src.core.schemas import UpdateMediaBuyRequest
from src.core.tools.media_buy_create import _validate_creatives_for_booking
from src.core.tools.media_buy_update import _reject_mismatched_creative_formats
from tests.factories.spec_required_kwargs import required_request_kwargs

AGENT_URL = "https://creative.adcontextprotocol.org"


def _fmt(width: int, height: int) -> dict:
    return {"agent_url": AGENT_URL, "id": "display_image", "width": width, "height": height}


def _creative(creative_id: str, width: int, height: int):
    return SimpleNamespace(
        creative_id=creative_id,
        agent_url=AGENT_URL,
        format="display_image",
        format_parameters={"width": width, "height": height},
    )


def _repo(product_formats: list[dict], creatives: list) -> MagicMock:
    """MediaBuyRepository stand-in: one package booked against one product."""
    repo = MagicMock()
    repo.get_package.return_value = SimpleNamespace(package_config={"product_id": "prod_1"})
    repo.get_product_by_id.return_value = SimpleNamespace(
        product_id="prod_1",
        name="Automotive",
        format_ids=product_formats,
        inventory_profile_id=None,
        inventory_profile=None,
    )
    repo.get_creatives_by_ids.return_value = creatives
    return repo


def _update_request(package: dict) -> UpdateMediaBuyRequest:
    return UpdateMediaBuyRequest(
        **required_request_kwargs(idempotency_key="fms2162-update-key"),
        media_buy_id="mb_1",
        packages=[{"package_id": "pkg_1", **package}],
    )


class TestUpdateRejectsMismatchedFormatsAtSubmission:
    @pytest.mark.parametrize(
        "package",
        [
            {"creative_ids": ["cr_300x250"]},
            {"creative_assignments": [{"creative_id": "cr_300x250"}]},
        ],
        ids=["creative_ids", "creative_assignments"],
    )
    def test_mismatched_creative_is_rejected(self, package):
        repo = _repo([_fmt(728, 90), _fmt(970, 250)], [_creative("cr_300x250", 300, 250)])

        with pytest.raises(AdCPValidationError) as exc_info:
            _reject_mismatched_creative_formats(_update_request(package), "mb_1", repo)

        assert exc_info.value.details["error_code"] == "INVALID_CREATIVES"
        assert len(exc_info.value.details["creative_errors"]) == 1
        assert "cr_300x250" in exc_info.value.message
        assert "Automotive" in exc_info.value.message

    def test_matching_creative_is_accepted(self):
        repo = _repo([_fmt(728, 90)], [_creative("cr_728x90", 728, 90)])

        _reject_mismatched_creative_formats(_update_request({"creative_ids": ["cr_728x90"]}), "mb_1", repo)

    def test_update_without_creatives_does_no_lookups(self):
        repo = _repo([_fmt(728, 90)], [])

        _reject_mismatched_creative_formats(_update_request({"paused": True}), "mb_1", repo)

        repo.get_creatives_by_ids.assert_not_called()

    def test_unknown_creative_is_left_to_the_apply_path(self):
        """Missing creative IDs keep their existing ``creatives_not_found`` error."""
        repo = _repo([_fmt(728, 90)], [])

        _reject_mismatched_creative_formats(_update_request({"creative_ids": ["cr_missing"]}), "mb_1", repo)


class TestManualApprovalDoesNotDeferMismatchedAssignments:
    """On tenants that route updates through manual approval, the check runs before deferral."""

    @staticmethod
    def _update(creative):
        """Run update_media_buy on a manual-approval tenant; return (result or error, step statuses)."""
        from tests.harness.media_buy_update import MediaBuyUpdateEnv

        with MediaBuyUpdateEnv() as env:
            env.set_media_buy()
            adapter = env.mock["adapter"].return_value
            adapter.manual_approval_required = True
            adapter.manual_approval_operations = ["update_media_buy"]
            repo = _repo([_fmt(728, 90)], [creative])
            media_buys = env.mock["uow"].return_value.media_buys
            media_buys.get_package = repo.get_package
            media_buys.get_product_by_id = repo.get_product_by_id
            media_buys.get_creatives_by_ids = repo.get_creatives_by_ids
            try:
                outcome = env.call_impl(
                    packages=[{"package_id": "pkg_1", "creative_assignments": [{"creative_id": creative.creative_id}]}]
                )
            except AdCPValidationError as exc:
                outcome = exc
            step_updates = env.mock["ctx_mgr"].return_value.update_workflow_step.call_args_list
            return outcome, [call.kwargs.get("status") for call in step_updates]

    def test_mismatched_assignment_is_rejected_not_deferred(self):
        outcome, step_statuses = self._update(_creative("cr_300x250", 300, 250))

        assert isinstance(outcome, AdCPValidationError)
        assert outcome.details["error_code"] == "INVALID_CREATIVES"
        assert step_statuses == ["failed"]

    def test_matching_assignment_is_still_deferred_for_approval(self):
        outcome, step_statuses = self._update(_creative("cr_728x90", 728, 90))

        assert outcome.workflow_step_id == "step_001"
        assert outcome.affected_packages == []
        assert step_statuses == ["requires_approval"]


class TestClickUrlRequiredOnEveryBookingPath:
    """``_validate_creatives_for_booking`` is the single pre-adapter gate for both booking paths."""

    NO_CLICK_URL = {"assets": {"image": {"url": "https://cdn.example.com/300x250.jpg"}}}
    WITH_CLICK_URL = {
        "assets": {
            "image": {"url": "https://cdn.example.com/300x250.jpg"},
            "click_url": {"url": "https://example.com", "url_type": "clickthrough", "asset_type": "url"},
        }
    }

    def _validate(self, creative_data: dict, tenant: dict) -> None:
        package = MagicMock()
        package.creative_ids = ["cr_1"]
        package.product_id = None

        creative = MagicMock()
        creative.creative_id = "cr_1"
        creative.format = "display_image"
        creative.agent_url = AGENT_URL
        creative.status = "approved"
        creative.data = creative_data

        format_spec = MagicMock()
        format_spec.output_format_ids = None
        format_spec.type = "display"

        session = MagicMock()
        session.scalars.return_value.all.return_value = [creative]

        with (
            patch("src.core.tools.media_buy_create._get_format_spec_sync", return_value=format_spec),
            patch(
                "src.core.tools.media_buy_create.extract_media_url_and_dimensions",
                return_value=("https://cdn.example.com/300x250.jpg", 300, 250),
            ),
        ):
            _validate_creatives_for_booking([package], tenant["tenant_id"], tenant, session)

    def test_improve_digital_rejects_creative_without_click_url(self):
        with pytest.raises(AdCPValidationError) as exc_info:
            self._validate(self.NO_CLICK_URL, {"tenant_id": "t1", "ad_server": "improvedigital"})

        assert exc_info.value.details["error_code"] == "INVALID_CREATIVES"
        assert "click-through URL" in exc_info.value.message

    def test_improve_digital_accepts_creative_with_click_url(self):
        self._validate(self.WITH_CLICK_URL, {"tenant_id": "t1", "ad_server": "improvedigital"})

    def test_other_ad_servers_do_not_require_click_url(self):
        self._validate(self.NO_CLICK_URL, {"tenant_id": "t1", "ad_server": "google_ad_manager"})
