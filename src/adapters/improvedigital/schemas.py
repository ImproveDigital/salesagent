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
    issued per client application. Campaign creation requires
    ``buying_entity_id`` + ``buying_entity_office_id`` (sandbox-confirmed:
    the server-side JSON schema requires the entity; a business rule then
    requires at least one office). Both are discoverable via the Admin API
    (``/admin/v1/buying-entities-combo`` → ``.../buying-entity-offices``)
    when the credentials carry admin scope. ``improve_demand_contact_id`` is
    optional on the wire — the selected office carries a default contact.
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
    buying_entity_id: int | None = Field(
        default=None,
        description="Buying entity the Classic campaigns book under — required on every campaign",
        json_schema_extra={"ui_order": 4},
    )
    buying_entity_office_id: int | None = Field(
        default=None,
        description=(
            "Buying entity office (currency-specific) assigned to every campaign — "
            "must belong to buying_entity_id and match the configured currency"
        ),
        json_schema_extra={"ui_order": 5},
    )
    improve_demand_contact_id: int | None = Field(
        default=None,
        description=(
            "Improve demand contact override — optional; when omitted the buying "
            "entity office's default contact applies"
        ),
        json_schema_extra={"ui_order": 6},
    )
    campaign_type: str = Field(
        default="Improve",
        description='Classic campaign "type" field — required by the campaign API ("Improve" for marketplace-booked campaigns)',
        json_schema_extra={"ui_order": 7},
    )
    default_advertiser_id: int | None = Field(
        default=None,
        description=(
            "Fallback advertiser ID for principals without an Improve Digital platform "
            "mapping — NOT part of the Classic campaign create schema; retained for the "
            "pending buyer-attribution mechanism (metadata-campaigns)"
        ),
        json_schema_extra={"ui_order": 8},
    )
    agency_id: int | None = Field(
        default=None,
        description="Metadata agency booking the campaign — CampaignMetadataDto.agencyId (pick via /api/metadata-agencies)",
        json_schema_extra={"ui_order": 9},
    )
    agency_name: str | None = Field(
        default=None,
        description="Display name of the selected agency — CampaignMetadataDto.agencyName",
        json_schema_extra={"ui_order": 9.1},
    )
    advertiser_uuid: str | None = Field(
        default=None,
        description=(
            "Metadata advertiser (brand) UUID — CampaignMetadataDto.advertiserUuid. "
            "Distinct from default_advertiser_id: the metadata surface keys brands by "
            "UUID, while CampaignDto.advertiserId is an integer"
        ),
        json_schema_extra={"ui_order": 9.2},
    )
    advertiser_name: str | None = Field(
        default=None,
        description="Display name of the selected advertiser — CampaignMetadataDto.advertiserName",
        json_schema_extra={"ui_order": 9.3},
    )
    integration_platform_id: int | None = Field(
        default=None,
        description=(
            "DSP the metadata record books under — CampaignMetadataDto.integrationPlatformId "
            "(pick via /api/metadata-integration-platforms; 1 = Improve Digital)"
        ),
        json_schema_extra={"ui_order": 9.35},
    )
    seat_id: str | None = Field(
        default=None,
        description=(
            "DSP seat on the metadata record — CampaignMetadataDto.seatId "
            "(string, e.g. 'default'; see /api/metadata-integration-platforms/{id}/seats)"
        ),
        json_schema_extra={"ui_order": 9.36},
    )
    adops_person_id: int | None = Field(
        default=None,
        description="Ad-ops owner for booked campaigns — CampaignMetadataDto.adOpsPersonId (integer)",
        json_schema_extra={"ui_order": 9.4},
    )
    sales_person_id: str | None = Field(
        default=None,
        description="Sales owner for booked campaigns — CampaignMetadataDto.salesPersonId (UUID string, not an integer)",
        json_schema_extra={"ui_order": 9.5},
    )
    business_unit_id: int | None = Field(
        default=None,
        description="Business unit ID — required on Classic line items (e.g. 33 = Azerion on the dev platform)",
        json_schema_extra={"ui_order": 10},
    )
    buyer_id: int | None = Field(
        default=None,
        description=(
            "Buyer the line items book under — sent as CommonDealLineItemDto.buyer_id "
            "(omitted when unset; the platform then derives it from the campaign)"
        ),
        json_schema_extra={"ui_order": 11},
    )
    currency: str = Field(
        default="EUR",
        description="Default campaign currency (ISO 4217)",
        json_schema_extra={
            "ui_order": 12,
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
        description="Default campaign timezone (IANA name, e.g. Europe/Amsterdam) — required by the campaign API",
        json_schema_extra={"ui_order": 13},
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
        description=(
            "Explicit creative-size override for line-item size targeting. "
            "Normally left empty — sizes are derived from the product's "
            "creative formats at buy time so the two can't diverge (M2)"
        ),
    )

    # -- Classic line-item defaults --
    geo_countries: list[str] = Field(
        default_factory=list,
        description=(
            "Default geo targeting: country names from the platform geo dictionary "
            "(/common/v1/countries), applied to every line item booked from this "
            "product (buyer overlays add on top)"
        ),
    )
    geo_regions: list[str] = Field(
        default_factory=list,
        description=(
            "Default geo targeting: region names from the platform geo dictionary "
            "(/rtb/v1/regions), applied to every line item booked from this product"
        ),
    )
    pricing_model: str | None = Field(
        default=None,
        description="Line-item pricing model (CPM confirmed; further values pending platform confirmation — gap G2)",
    )
    goal: str = Field(
        default="BUDGET",
        description=(
            "Line-item delivery goal. BUDGET books against the package budget "
            "(sent as budget + flight_details); IMPRESSION books against the "
            "impression cap derived from budget ÷ rate"
        ),
        json_schema_extra={"enum": ["BUDGET", "IMPRESSION"]},
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
