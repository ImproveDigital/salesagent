"""Tests for Improve Digital adapter schemas — encryption round-trip + validation."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.adapters.improvedigital.schemas import ImproveDigitalConnectionConfig, ImproveDigitalProductConfig
from src.core.utils.encryption import generate_encryption_key, is_encrypted


@pytest.fixture
def encryption_key():
    key = generate_encryption_key()
    with patch.dict(os.environ, {"ENCRYPTION_KEY": key}):
        yield key


class TestConnectionConfig:
    """OAuth2 client_credentials is the only auth path."""

    def test_accepts_client_credentials(self):
        cfg = ImproveDigitalConnectionConfig(client_id="app-1", client_secret="hunter2")
        assert cfg.client_id == "app-1"
        assert cfg.client_secret == "hunter2"
        assert cfg.api_base_url == "https://api.360yield.com"
        assert cfg.currency == "EUR"
        assert cfg.timezone == "UTC"
        assert cfg.improve_demand_contact_id is None
        assert cfg.default_advertiser_id is None
        assert cfg.agency_id is None

    def test_client_secret_serializes_to_ciphertext(self, encryption_key):
        cfg = ImproveDigitalConnectionConfig(client_id="app-1", client_secret="super-secret")
        dumped = cfg.model_dump()
        assert dumped["client_secret"] != "super-secret"
        assert is_encrypted(dumped["client_secret"])

    def test_client_secret_round_trips_through_dump_and_validate(self, encryption_key):
        original = ImproveDigitalConnectionConfig(client_id="app-1", client_secret="super-secret")
        persisted = original.model_dump()
        rehydrated = ImproveDigitalConnectionConfig.model_validate(persisted)
        assert rehydrated.client_secret == "super-secret"

    def test_already_encrypted_secret_not_double_encrypted(self, encryption_key):
        cfg = ImproveDigitalConnectionConfig(client_id="app-1", client_secret="super-secret")
        ciphertext = cfg.model_dump()["client_secret"]
        rehydrated = ImproveDigitalConnectionConfig.model_validate({"client_id": "app-1", "client_secret": ciphertext})
        assert rehydrated.client_secret == "super-secret"

    def test_missing_credentials_rejected(self):
        with pytest.raises(ValidationError, match="client_id \\+ client_secret"):
            ImproveDigitalConnectionConfig()

    def test_partial_credentials_rejected(self):
        with pytest.raises(ValidationError, match="client_id \\+ client_secret"):
            ImproveDigitalConnectionConfig(client_id="app-1")

    def test_non_https_base_url_rejected(self):
        with pytest.raises(ValidationError, match="https"):
            ImproveDigitalConnectionConfig(
                client_id="app-1",
                client_secret="s",
                api_base_url="http://api.360yield.com",
            )

    def test_secret_flag_visible_in_json_schema(self):
        schema = ImproveDigitalConnectionConfig.model_json_schema()
        assert schema["properties"]["client_secret"]["secret"] is True

    def test_classic_defaults_round_trip(self, encryption_key):
        cfg = ImproveDigitalConnectionConfig(
            client_id="app-1",
            client_secret="s",
            improve_demand_contact_id=7,
            default_advertiser_id=42,
            agency_id=9,
            currency="USD",
            timezone="Europe/Amsterdam",
        )
        rehydrated = ImproveDigitalConnectionConfig.model_validate(cfg.model_dump())
        assert rehydrated.improve_demand_contact_id == 7
        assert rehydrated.default_advertiser_id == 42
        assert rehydrated.agency_id == 9
        assert rehydrated.currency == "USD"
        assert rehydrated.timezone == "Europe/Amsterdam"


class TestProductConfig:
    def test_defaults_are_empty(self):
        cfg = ImproveDigitalProductConfig()
        assert cfg.placement_ids == []
        assert cfg.excluded_placement_ids == []
        assert cfg.package_ids == []
        assert cfg.size_ids == []
        assert cfg.pricing_model is None
        assert cfg.frequency_cap is None
        assert cfg.seller_types == []

    def test_full_envelope_round_trips(self):
        cfg = ImproveDigitalProductConfig(
            placement_ids=[1, 2],
            excluded_placement_ids=[3],
            package_ids=[4],
            size_ids=[5, 6],
            pricing_model="CPM",
            frequency_cap=3,
            frequency_interval=1,
            frequency_interval_type="days",
            delivery_schedule="even",
            azerion_owned=True,
            seller_types=["PUBLISHER"],
            iab_categories=[100],
            tier_ids=[2],
        )
        rehydrated = ImproveDigitalProductConfig.model_validate(cfg.model_dump())
        assert rehydrated == cfg

    def test_unknown_fields_rejected(self):
        with pytest.raises(ValidationError):
            ImproveDigitalProductConfig(not_a_field=True)
