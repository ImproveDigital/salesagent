"""Secret-field handling in the generic adapter-config save path.

Covers the pure helpers behind ``save_adapter_config``: schema-driven
discovery of ``secret``-marked fields and carrying stored secrets forward
when the admin form leaves them blank ("keep existing").
"""

from __future__ import annotations

import pytest

from src.adapters.improvedigital import ImproveDigitalConnectionConfig
from src.admin.blueprints.adapters import (
    _preserve_omitted_secret_fields,
    _secret_fields_for_connection_schema,
)

pytestmark = pytest.mark.unit


class TestSecretFieldDiscovery:
    def test_improvedigital_schema_marks_client_secret(self):
        assert _secret_fields_for_connection_schema(ImproveDigitalConnectionConfig) == ["client_secret"]

    def test_no_schema_means_no_secret_fields(self):
        assert _secret_fields_for_connection_schema(None) == []


class TestPreserveOmittedSecrets:
    def test_omitted_secret_is_carried_forward_from_stored_config(self):
        config_data = {"client_id": "app-1"}
        _preserve_omitted_secret_fields(
            config_data, {"client_id": "app-1", "client_secret": "stored-cipher"}, secret_fields=["client_secret"]
        )
        assert config_data["client_secret"] == "stored-cipher"

    def test_submitted_secret_wins_over_stored(self):
        config_data = {"client_id": "app-1", "client_secret": "fresh-plain"}
        _preserve_omitted_secret_fields(
            config_data, {"client_secret": "stored-cipher"}, secret_fields=["client_secret"]
        )
        assert config_data["client_secret"] == "fresh-plain"

    def test_nothing_stored_leaves_field_absent(self):
        config_data = {"client_id": "app-1"}
        _preserve_omitted_secret_fields(config_data, {}, secret_fields=["client_secret"])
        assert "client_secret" not in config_data
