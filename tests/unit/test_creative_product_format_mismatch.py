"""Creative vs product format/size mismatch checks (FMS-2154).

Covers the shared building blocks behind the "wrong creative" fixes:
- which formats a product is validated against (inventory profile vs own column)
- the mismatch description shown in the admin UI and used to block approval
- the declared-size vs asset-size check in sync_creatives
"""

from types import SimpleNamespace

import pytest

from src.core.canonical_formats import product_validation_format_ids
from src.core.helpers.creative_helpers import (
    stored_creative_format_mismatch,
    validate_creative_format_against_product,
)
from src.core.schemas import FormatId
from src.core.tools.creatives._processing import _declared_size_mismatch_result

AGENT_URL = "https://creative.adcontextprotocol.org"


def _fmt(width: int, height: int, format_id: str = "display_image") -> dict:
    return {"agent_url": AGENT_URL, "id": format_id, "width": width, "height": height}


def _product(format_ids, *, profile_format_ids=None, name="Automotive"):
    """Product stand-in; linked to an inventory profile when profile_format_ids is given."""
    if profile_format_ids is None:
        return SimpleNamespace(
            product_id="prod_1", name=name, format_ids=format_ids, inventory_profile_id=None, inventory_profile=None
        )
    return SimpleNamespace(
        product_id="prod_1",
        name=name,
        format_ids=format_ids,
        inventory_profile_id=7,
        inventory_profile=SimpleNamespace(format_ids=profile_format_ids),
    )


def _creative(width: int | None, height: int | None, format_id: str = "display_image"):
    params = {"width": width, "height": height} if width and height else None
    return SimpleNamespace(creative_id="cr_1", agent_url=AGENT_URL, format=format_id, format_parameters=params)


def _format_info(width: int | None, height: int | None, format_id: str = "display_image") -> dict:
    params = {"width": width, "height": height} if width and height else None
    return {"agent_url": AGENT_URL, "format_id": format_id, "parameters": params}


def _image_data(width: int, height: int) -> dict:
    return {
        "assets": {
            "banner_image": {
                "asset_type": "image",
                "url": "https://cdn.example.com/a.png",
                "width": width,
                "height": height,
            }
        }
    }


class TestProductValidationFormatIds:
    def test_uses_own_formats_without_profile(self):
        product = _product([_fmt(728, 90)])
        assert product_validation_format_ids(product) == [_fmt(728, 90)]

    def test_uses_profile_formats_when_linked(self):
        product = _product([], profile_format_ids=[_fmt(970, 250)])
        assert product_validation_format_ids(product) == [_fmt(970, 250)]

    def test_profile_formats_override_stale_own_formats(self):
        product = _product([_fmt(300, 250)], profile_format_ids=[_fmt(970, 250)])
        assert product_validation_format_ids(product) == [_fmt(970, 250)]

    def test_none_formats_become_empty_list(self):
        assert product_validation_format_ids(_product(None)) == []


class TestValidateCreativeFormatAgainstProfileLinkedProduct:
    def test_rejects_format_missing_from_profile_even_if_own_column_is_empty(self):
        product = _product([], profile_format_ids=[_fmt(728, 90)])
        creative_format = FormatId(agent_url=AGENT_URL, id="display_image", width=300, height=250)

        is_valid, error = validate_creative_format_against_product(creative_format, product)

        assert is_valid is False
        assert error is not None and "Automotive" in error

    def test_accepts_format_present_in_profile(self):
        product = _product([], profile_format_ids=[_fmt(728, 90)])
        creative_format = FormatId(agent_url=AGENT_URL, id="display_image", width=728, height=90)

        assert validate_creative_format_against_product(creative_format, product) == (True, None)


class TestStoredCreativeFormatMismatch:
    def test_wrong_size_is_described(self):
        product = _product([_fmt(728, 90), _fmt(970, 250)])

        mismatch = stored_creative_format_mismatch(_creative(300, 250), product)

        assert mismatch == (
            "Creative format display_image 300x250 is not accepted by product 'Automotive' "
            "(accepts: display_image 728x90, display_image 970x250)"
        )

    def test_matching_size_is_not_flagged(self):
        product = _product([_fmt(728, 90), _fmt(970, 250)])
        assert stored_creative_format_mismatch(_creative(728, 90), product) is None

    def test_legacy_fixed_size_id_matches_canonical_product_format(self):
        product = _product([_fmt(300, 250)])
        assert stored_creative_format_mismatch(_creative(None, None, "display_300x250"), product) is None

    def test_product_without_formats_accepts_everything(self):
        assert stored_creative_format_mismatch(_creative(300, 250), _product([])) is None

    def test_profile_linked_product_is_checked_against_profile_formats(self):
        product = _product([_fmt(300, 250)], profile_format_ids=[_fmt(728, 90)])
        assert stored_creative_format_mismatch(_creative(300, 250), product) is not None
        assert stored_creative_format_mismatch(_creative(728, 90), product) is None


class TestDeclaredSizeMismatch:
    def test_asset_size_differs_from_declared_size_fails(self):
        result = _declared_size_mismatch_result("cr_1", _format_info(728, 90), _image_data(300, 250))

        assert result is not None
        assert result.creative_id == "cr_1"
        assert getattr(result.action, "value", result.action) == "failed"
        assert result.errors is not None
        assert result.errors[0].code == "creative_dimension_mismatch"
        assert "300x250" in result.errors[0].message and "728x90" in result.errors[0].message

    def test_root_level_size_differs_from_declared_size_fails(self):
        data = {"url": "https://cdn.example.com/a.png", "width": 300, "height": 250}
        result = _declared_size_mismatch_result("cr_1", _format_info(728, 90), data)

        assert result is not None
        assert result.errors is not None
        assert result.errors[0].code == "creative_dimension_mismatch"

    def test_matching_size_passes(self):
        assert _declared_size_mismatch_result("cr_1", _format_info(300, 250), _image_data(300, 250)) is None

    def test_no_declared_size_passes(self):
        assert _declared_size_mismatch_result("cr_1", _format_info(None, None), _image_data(300, 250)) is None

    def test_no_asset_size_passes(self):
        data = {"url": "https://cdn.example.com/a.png"}
        assert _declared_size_mismatch_result("cr_1", _format_info(728, 90), data) is None

    def test_non_display_format_is_not_checked(self):
        result = _declared_size_mismatch_result(
            "cr_1", _format_info(640, 360, "video_standard"), _image_data(1920, 1080)
        )
        assert result is None


class TestUpdateMediaBuyCreativeAssignmentsFormatCheck:
    """update_media_buy creative_assignments must validate creative format against the package product."""

    @staticmethod
    def _call(creative, product):
        from tests.harness.media_buy_update import MediaBuyUpdateEnv

        with MediaBuyUpdateEnv() as env:
            env.set_media_buy()
            media_buys = env.mock["uow"].return_value.media_buys
            media_buys.get_package.return_value = SimpleNamespace(package_config={"product_id": "prod_1"})
            media_buys.get_product_by_id.return_value = product
            media_buys.get_creatives_by_ids.return_value = [creative]
            result = env.call_impl(
                packages=[{"package_id": "pkg_1", "creative_assignments": [{"creative_id": "cr_1"}]}]
            )
            return result, media_buys

    def test_mismatching_creative_is_rejected(self):
        from src.core.exceptions import AdCPValidationError

        with pytest.raises(AdCPValidationError) as exc_info:
            self._call(_creative(300, 250), _product([_fmt(728, 90)]))

        assert exc_info.value.details["error_code"] == "INVALID_CREATIVES"
        assert "cr_1" in exc_info.value.details["creative_errors"][0]

    def test_mismatch_against_inventory_profile_formats_is_rejected(self):
        from src.core.exceptions import AdCPValidationError

        with pytest.raises(AdCPValidationError):
            self._call(_creative(300, 250), _product([_fmt(300, 250)], profile_format_ids=[_fmt(728, 90)]))

    def test_matching_creative_is_accepted(self):
        from src.core.schemas import UpdateMediaBuyError

        result, media_buys = self._call(_creative(728, 90), _product([_fmt(728, 90)]))

        media_buys.get_creatives_by_ids.assert_called_once_with(["cr_1"])
        assert not isinstance(result, UpdateMediaBuyError)
