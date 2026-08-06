"""Pydantic config schemas for the Improve Digital adapter.

``ImproveDigitalConnectionConfig`` — tenant-level credentials + defaults,
persisted (Fernet-encrypted secrets) in ``AdapterConfig.config_json``.

``ImproveDigitalProductConfig`` — per-product inventory selection and
delivery defaults, stored on the product's ``implementation_config``.

Both extend the shared base classes so the admin UI / tenant-management API
can introspect them generically (``json_schema_extra`` drives field ordering
and secret masking).
"""

from __future__ import annotations

from pydantic import Field, field_serializer, field_validator, model_validator

from src.adapters._secret_fields import decrypt_secret_value, encrypt_secret_value
from src.adapters.base import BaseConnectionConfig, BaseProductConfig
from src.adapters.improvedigital._transport import DEFAULT_BASE_URL

# Placement-search seller_types filter values (from the rtb/v3 OpenAPI spec).
IMPROVEDIGITAL_SELLER_TYPES = ["PUBLISHER", "INTERMEDIARY", "BOTH"]


class ImproveDigitalConnectionConfig(BaseConnectionConfig):
    """Connection settings for the 360Yield Marketplace API (Classic campaigns).

    Auth is OAuth2 client_credentials only — one client id/secret pair is
    issued per client application. ``improve_demand_contact_id`` is required
    by the Classic campaign API on every campaign; ``default_advertiser_id``
    backs principals without their own advertiser mapping.
    """

    client_id: str | None = Field(
        default=None,
        description="OAuth2 client ID issued by Improve Digital",
        json_schema_extra={"ui_order": 1},
    )
    client_secret: str | None = Field(
        default=None,
        description="OAuth2 client secret",
        json_schema_extra={"secret": True, "ui_order": 2},
    )
    api_base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description="360Yield API host (override for testing only)",
        json_schema_extra={"ui_order": 3},
    )
    improve_demand_contact_id: int | None = Field(
        default=None,
        description="Improve demand contact ID — required by the Classic campaign API on every campaign",
        json_schema_extra={"ui_order": 4},
    )
    default_advertiser_id: int | None = Field(
        default=None,
        description="Fallback advertiser ID for principals without an Improve Digital platform mapping",
        json_schema_extra={"ui_order": 5},
    )
    agency_id: int | None = Field(
        default=None,
        description="Default agency ID applied to Classic campaigns (optional)",
        json_schema_extra={"ui_order": 6},
    )
    buying_entity_id: int | None = Field(
        default=None,
        description=(
            "Buying entity ID — required by the Classic campaign API "
            "(e.g. 421 = 'Improve Digital Marketplace' on the dev platform)"
        ),
        json_schema_extra={"ui_order": 7},
    )
    buying_entity_office_id: int | None = Field(
        default=None,
        description="Buying entity office (buyer seat) ID applied to Classic campaigns",
        json_schema_extra={"ui_order": 8},
    )
    business_unit_id: int | None = Field(
        default=None,
        description="Business unit ID — required on Classic line items (e.g. 33 = Azerion on the dev platform)",
        json_schema_extra={"ui_order": 9},
    )
    currency: str = Field(
        default="EUR",
        description="Default campaign currency (ISO 4217)",
        json_schema_extra={
            "ui_order": 10,
            # CampaignDto currency enum from the rtb/v3 OpenAPI spec.
            "enum": [
                "EUR",
                "USD",
                "GBP",
                "DKK",
                "SEK",
                "AUD",
                "CZK",
                "HUF",
                "CHF",
                "NOK",
                "SGD",
                "HKD",
                "MYR",
                "CAD",
                "TRY",
            ],
        },
    )
    timezone: str = Field(
        default="UTC",
        description="Default campaign timezone",
        json_schema_extra={"ui_order": 11},
    )

    @field_serializer("client_secret")
    def _encrypt_client_secret(self, value: str | None) -> str | None:
        return encrypt_secret_value(value)

    @field_validator("client_secret", mode="after")
    @classmethod
    def _decrypt_client_secret(cls, value: str | None) -> str | None:
        return decrypt_secret_value(value)

    @field_validator("api_base_url", mode="after")
    @classmethod
    def _base_url_must_be_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("api_base_url must be an https:// URL")
        return value

    @model_validator(mode="after")
    def _require_credentials(self) -> ImproveDigitalConnectionConfig:
        if not (self.client_id and self.client_secret):
            raise ValueError("Improve Digital connection requires client_id + client_secret")
        return self


class ImproveDigitalProductConfig(BaseProductConfig):
    """Per-product inventory selection + Classic line-item defaults.

    Static selection pins line items to explicit placements/packages/sizes;
    the search-filter fields are defaults for placement discovery in the
    product-config UI (they are not sent on the line item itself).
    """

    # -- static inventory selection (assigned to the line item) --
    placement_ids: list[int] = Field(
        default_factory=list,
        description="Placement IDs assigned to the line item",
    )
    excluded_placement_ids: list[int] = Field(
        default_factory=list,
        description="Placement IDs explicitly excluded from the line item",
    )
    package_ids: list[int] = Field(
        default_factory=list,
        description="Reusable placement-package IDs assigned to the line item",
    )
    size_ids: list[int] = Field(
        default_factory=list,
        description="Creative size IDs targeted by the line item",
    )

    # -- Classic line-item defaults --
    pricing_model: str | None = Field(
        default=None,
        description="Line-item pricing model (CPM confirmed; further values pending platform confirmation — gap G2)",
    )
    frequency_cap: int | None = Field(
        default=None,
        description="Impressions per user per frequency interval",
    )
    frequency_interval: float | None = Field(
        default=None,
        description="Frequency-cap interval count",
    )
    frequency_interval_type: str | None = Field(
        default=None,
        description="Frequency-cap interval unit (months/weeks/days/hours/minutes)",
    )
    delivery_schedule: str | None = Field(
        default=None,
        description="Line-item delivery pacing schedule",
    )

    # -- placement-search filter defaults (product-config UI only) --
    azerion_owned: bool | None = Field(
        default=None,
        description="Placement search default: restrict to Azerion-owned inventory",
    )
    seller_types: list[str] = Field(
        default_factory=list,
        description=f"Placement search default: seller types ({'/'.join(IMPROVEDIGITAL_SELLER_TYPES)})",
    )
    iab_categories: list[int] = Field(
        default_factory=list,
        description="Placement search default: IAB category IDs",
    )
    tier_ids: list[int] = Field(
        default_factory=list,
        description="Placement search default: inventory tier IDs",
    )
