"""Improve Digital creatives carry a mandatory IAB v1 category.

``CreativeDto.iab_category`` is required by the Marketplace creative form and
bulk-upload templates. AdCP has no IAB field, so the adapter derives it from
the buy's ``advertiser_industry`` and falls back to the tenant's
``default_iab_category`` connection setting. A creative with no resolvable
category is rejected explicitly, and a buy carrying creatives is refused
before any campaign is created upstream.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.adapters.improvedigital import ImproveDigitalAdapter
from src.adapters.improvedigital.iab import IAB_CATEGORY_PATTERN, iab_category_for_industry
from src.adapters.improvedigital.schemas import ImproveDigitalConnectionConfig
from src.core.schemas import CreateMediaBuyError
from tests.helpers.adapter_test_helpers import (
    invoke_create_media_buy,
    make_sample_create_request,
    make_sample_video_package,
)

LIVE_CONFIG = {
    "client_id": "app-1",
    "client_secret": "s3cret",
    "improve_demand_contact_id": 17918,
    "buying_entity_id": 421,
    "business_unit_id": 33,
    "api_base_url": "https://api.360yielddev.example",
}


class _Principal:
    name = "classic_advertiser"
    principal_id = "principal_impd_1"
    platform_mappings = {"improvedigital": {"advertiser_id": "5001"}}

    def get_adapter_id(self, adapter: str) -> str | None:
        return "5001" if adapter == "improvedigital" else None


def _adapter(**config) -> ImproveDigitalAdapter:
    return ImproveDigitalAdapter(config=config, principal=_Principal(), dry_run=True, tenant_id="tenant_impd_1")


def _image_asset(**overrides) -> dict:
    asset = {
        "creative_id": "cr_1",
        "name": "Azerion 728x90",
        "asset_type": "image",
        "url": "https://cdn.example.com/banner.jpg",
        "click_url": "https://azerion.com",
        "width": 728,
        "height": 90,
    }
    asset.update(overrides)
    return asset


class TestIndustryMapping:
    def test_exact_industry_maps_to_sub_category(self):
        assert iab_category_for_industry("gaming") == "IAB9-30"
        assert iab_category_for_industry("travel_hospitality.airlines") == "IAB20-3"

    def test_unmapped_sub_industry_falls_back_to_parent(self):
        assert iab_category_for_industry("finance.banking") == "IAB13"

    def test_unmapped_or_missing_industry_returns_none(self):
        assert iab_category_for_industry("cannabis") is None
        assert iab_category_for_industry(None) is None

    def test_every_mapped_value_is_a_valid_iab_v1_code(self):
        from src.adapters.improvedigital.iab import ADVERTISER_INDUSTRY_TO_IAB

        assert all(IAB_CATEGORY_PATTERN.match(code) for code in ADVERTISER_INDUSTRY_TO_IAB.values())


class TestDefaultIabCategoryConfig:
    def test_valid_code_is_normalised(self):
        config = ImproveDigitalConnectionConfig(**LIVE_CONFIG, default_iab_category=" iab19-6 ")
        assert config.default_iab_category == "IAB19-6"

    def test_invalid_code_is_rejected(self):
        with pytest.raises(ValidationError, match="IAB v1 code"):
            ImproveDigitalConnectionConfig(**LIVE_CONFIG, default_iab_category="Technology")


class TestCreativeIabCategory:
    def test_payload_carries_category_from_advertiser_industry(self):
        adapter = _adapter()
        payload = adapter._creative_payload(_image_asset(advertiser_industry="technology.software"))
        assert payload is not None
        assert payload["iab_category"] == "IAB19"

    def test_tenant_default_used_when_industry_unmapped(self):
        adapter = _adapter(default_iab_category="IAB24")
        payload = adapter._creative_payload(_image_asset(advertiser_industry="cannabis"))
        assert payload is not None
        assert payload["iab_category"] == "IAB24"

    def test_creative_without_resolvable_category_is_rejected(self):
        adapter = _adapter()
        statuses = adapter.add_creative_assets("improvedigital_123", [_image_asset()], datetime.now(UTC))
        assert statuses[0].status == "failed"
        assert "IAB category" in statuses[0].message


class TestCreateMediaBuyRefusesWithoutCategory:
    def _live_adapter(self, **extra) -> ImproveDigitalAdapter:
        adapter = ImproveDigitalAdapter(
            config={**LIVE_CONFIG, **extra}, principal=_Principal(), dry_run=False, tenant_id="tenant_impd_1"
        )
        adapter._client = object()  # never reached: the refusal happens first
        return adapter

    def _request_with_creatives(self):
        request = make_sample_create_request()
        request.packages[0].creative_ids = ["cr_1"]
        return request

    def test_buy_with_creatives_and_no_category_is_refused_before_booking(self):
        adapter = self._live_adapter()
        with patch.object(adapter, "_office_for_currency", return_value=635):
            response = invoke_create_media_buy(adapter, self._request_with_creatives(), [make_sample_video_package()])
        assert isinstance(response, CreateMediaBuyError)
        assert response.errors[0].code == "iab_category_required"

    def test_tenant_default_lets_the_buy_through_the_category_check(self):
        adapter = self._live_adapter(default_iab_category="IAB19")
        # Stop at the next guard so no upstream call is attempted: reaching it
        # proves the category check passed.
        with (
            patch.object(adapter, "_office_for_currency", return_value=635),
            patch.object(adapter, "_missing_booking_config", return_value=["stop here"]),
        ):
            response = invoke_create_media_buy(adapter, self._request_with_creatives(), [make_sample_video_package()])
        assert isinstance(response, CreateMediaBuyError)
        assert response.errors[0].code == "incomplete_adapter_config"
