"""Improve Digital adapter — implements ``AdServerAdapter`` against the
360Yield Marketplace API (Azerion / 360 Polaris), targeting **Classic
(direct) campaigns** in Phase 1: campaigns whose creatives are hosted and
served by the Improve adserver.

Entity mapping (confirmed against the Marketplace API docs + OpenAPI spec,
see docs/adapters/improvedigital/INTEGRATION_PLAN.md):

- AdCP MediaBuy → Classic Campaign (``POST /rtb/v1/classic/campaigns``)
- AdCP Package  → Classic Line Item (``POST .../campaigns/{id}/line-items``)
- AdCP Creative → Classic Creative (``POST .../campaigns/{id}/creatives``)
  + line-item binding (``PUT .../line-items/{id}/creatives``)
- Delivery      → Report API ``EXT_CONSOLIDATE`` (definitive) +
  ``impression-delivery`` (real-time trend, not source of truth)

Live coverage (skeleton status — credentials pending, blocker B2):

- ✅ dry-run for every tool path — echoes the Classic campaign / line-item /
  creative payloads the adapter would send.
- ✅ check_media_buy_status — reads ``GET /rtb/v1/classic/campaigns/{id}``.
- ✅ check_permissions — token mint + read probes per scope.
- ⏳ create_media_buy / update_media_buy / creative live paths return
  ``pending_credentials`` until the Classic wire shapes are validated
  against real credentials (M2).
- ⏳ get_media_buy_delivery raises ``DeliveryDataUnavailable`` until the
  Report API cache lands (Phase 3 / M3).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from src.adapters.base import (
    AdapterCapabilities,
    AdServerAdapter,
    CreativeEngineAdapter,
    DeliveryDataUnavailable,
    PermissionsReport,
    TargetingCapabilities,
)
from src.adapters.constants import REQUIRED_UPDATE_ACTIONS
from src.adapters.improvedigital.client import ImproveDigitalAuthError, ImproveDigitalClient, ImproveDigitalError
from src.adapters.improvedigital.formats import improvedigital_creative_formats
from src.adapters.improvedigital.schemas import ImproveDigitalConnectionConfig, ImproveDigitalProductConfig
from src.adapters.improvedigital.targeting import build_targeting, validate_targeting
from src.core.schemas import (
    AdapterGetMediaBuyDeliveryResponse,
    AssetStatus,
    CheckMediaBuyStatusResponse,
    CreateMediaBuyError,
    CreateMediaBuyRequest,
    CreateMediaBuyResponse,
    Error,
    MediaPackage,
    Principal,
    ReportingPeriod,
    UpdateMediaBuyResponse,
    UpdateMediaBuySuccess,
)

logger = logging.getLogger(__name__)

_PENDING_CREDS_MESSAGE = (
    "Improve Digital live-mode operations require API credentials (OAuth2 "
    "client id/secret — integration blocker B2) and validated Classic wire "
    "shapes. Run in dry-run mode until provisioning completes."
)


class ImproveDigitalAdapter(AdServerAdapter):
    """AdCP adapter for Improve Digital Classic (direct) campaigns."""

    adapter_name = "improvedigital"
    default_channels = ["display", "olv", "audio", "native"]
    default_delivery_measurement = {"provider": "improvedigital"}
    connection_config_class = ImproveDigitalConnectionConfig
    product_config_class = ImproveDigitalProductConfig
    capabilities = AdapterCapabilities(
        # Flip alongside the Phase 2 placement cache (inventory_sync.py).
        supports_inventory_sync=False,
        # Flip alongside the Phase 3 Report API cache (reporting_sync.py).
        supports_reporting_sync=False,
        inventory_entity_label="Placements",
        supports_custom_targeting=True,
        supports_geo_targeting=True,
        supports_dynamic_products=False,
        # CPM confirmed (line items carry cpm_bid; cpc_bid also exists but
        # allowed pricing_model values are unconfirmed — gap G2).
        supported_pricing_models=["cpm"],
        supports_webhooks=False,
        supports_realtime_reporting=False,
    )

    def __init__(
        self,
        config: dict[str, Any],
        principal: Principal,
        dry_run: bool = False,
        creative_engine: CreativeEngineAdapter | None = None,
        tenant_id: str | None = None,
    ):
        """Resolve advertiser identity + OAuth credentials and target host.

        Dry-run defers client construction so the adapter can be configured
        before Improve Digital issues API credentials (blocker B2).
        """
        super().__init__(config, principal, dry_run, creative_engine, tenant_id)

        # Classic buyer identity: the campaign's advertiser. Principal
        # mapping wins; the tenant-level connection default backs it.
        self.advertiser_id = self.principal.get_adapter_id("improvedigital") or self.config.get("default_advertiser_id")
        if not self.advertiser_id and not self.dry_run:
            raise ValueError(
                f"Principal {principal.principal_id} does not have an Improve Digital "
                "advertiser ID and no default_advertiser_id is configured"
            )
        # Required by the Classic campaign API on every campaign.
        self.improve_demand_contact_id = self.config.get("improve_demand_contact_id")
        self.agency_id = self.config.get("agency_id")

        self.client_id = self.config.get("client_id")
        self.client_secret = self.config.get("client_secret")
        self.base_url = (self.config.get("api_base_url") or "https://api.360yield.com").rstrip("/")
        self.currency = self.config.get("currency", "EUR")
        self.timezone = self.config.get("timezone", "UTC")

        if self.dry_run:
            self.log(
                "Running in dry-run mode — Improve Digital API calls will be simulated",
                dry_run_prefix=False,
            )
            self._client: ImproveDigitalClient | None = None
        else:
            if not (self.client_id and self.client_secret):
                raise ValueError("Improve Digital config requires client_id + client_secret")
            if not self.improve_demand_contact_id:
                raise ValueError(
                    "Improve Digital config requires improve_demand_contact_id — the Classic "
                    "campaign API rejects campaigns without it"
                )
            self._client = ImproveDigitalClient(
                client_id=self.client_id,
                client_secret=self.client_secret,
                base_url=self.base_url,
            )

    # ----- capabilities -----

    def get_supported_pricing_models(self) -> set[str]:
        return {"cpm"}

    def get_creative_formats(self) -> list[dict[str, Any]]:
        return improvedigital_creative_formats(self.tenant_id)

    def get_targeting_capabilities(self) -> TargetingCapabilities:
        # Location targeting reaches city level; no postal-code support on
        # the platform, so every postal field stays False.
        return TargetingCapabilities(
            geo_countries=True,
            geo_regions=True,
        )

    # ----- permissions -----

    def check_permissions(self) -> PermissionsReport:
        """Mint a token and probe one read endpoint per adapter concern."""
        report = self._new_permissions_report(
            dry_run_message="Dry-run mode — Improve Digital permissions were not probed"
        )
        if self.dry_run:
            return report
        assert self._client is not None

        probes = [
            (
                "classic_campaigns_read",
                "List Classic campaigns",
                "GET",
                "/rtb/v1/classic/campaigns?limit=1",
                True,
                "media buy lifecycle",
            ),
            (
                "classic_line_items_read",
                "List Classic line items",
                "GET",
                "/rtb/v1/classic/line-items?limit=1",
                True,
                "media buy lifecycle",
            ),
            ("placements_read", "Placement search", "GET", "/rtb/v3/placements?limit=1", True, "inventory"),
            (
                "classic_campaign_schema",
                "Classic campaign creation schema",
                "GET",
                "/schema/rtb/v1/classic/campaigns/campaign",
                True,
                "create_media_buy",
            ),
            ("sizes_read", "Size lookups", "GET", "/rtb/v1/sizes-all", False, "creative formats"),
        ]

        def _probe(method: str, path: str) -> tuple[int, str]:
            assert self._client is not None
            return self._client.probe(method, path)

        try:
            self._walk_permission_probes(report, probes, _probe, auth_error_types=(ImproveDigitalAuthError,))
        except ImproveDigitalError as exc:
            report.error = f"Permission probe failed: {exc}"
        return report

    # ----- create_media_buy -----

    def create_media_buy(
        self,
        request: CreateMediaBuyRequest,
        packages: list[MediaPackage],
        start_time: datetime,
        end_time: datetime,
        package_pricing_info: dict[str, dict] | None = None,
    ) -> CreateMediaBuyResponse:
        self._audit_create_media_buy(request, start_time, end_time)

        targeting_error = self._validate_targeting_or_error(
            packages, validate_targeting, adapter_name="Improve Digital"
        )
        if targeting_error is not None:
            return targeting_error

        buy_name = self._buy_name(request)

        if self.dry_run:
            campaign_payload = self._campaign_payload(buy_name, start_time, end_time)
            self.log(f"Would call: POST {self.base_url}/rtb/v1/classic/campaigns")
            self.log(f"  Campaign: {campaign_payload}")
            for package in packages:
                rate, rate_type = self._resolve_pricing_rate(package, package_pricing_info)
                payload = self._line_item_payload(package, rate, rate_type, start_time, end_time)
                self.log(f"Would call: POST {self.base_url}/rtb/v1/classic/campaigns/<new>/line-items")
                self.log(f"  LineItem: {payload}")
                product_config = self._product_config_from_package(package)
                if product_config.get("placement_ids") or product_config.get("package_ids"):
                    self.log(
                        "Would call: PUT .../line-items/<new>/placements/assign "
                        f"placement_ids={product_config.get('placement_ids', [])} "
                        f"package_ids={product_config.get('package_ids', [])}"
                    )
            return self._build_create_success(request, f"improvedigital_{buy_name}", packages)

        # Live mode — pending API credentials (blocker B2) and wire-shape
        # validation. Lands in milestone M2 of the integration plan.
        return self._pending_creds_error()

    def _campaign_payload(self, buy_name: str, start_time: datetime, end_time: datetime) -> dict[str, Any]:
        """Build a ``CampaignDto`` body (required: name, start_date,
        improve_demand_contact_id)."""
        payload: dict[str, Any] = {
            "name": buy_name,
            "start_date": start_time.date().isoformat(),
            "end_date": end_time.date().isoformat(),
            "time_zone": self.timezone,
            "currency": self.currency,
            "improve_demand_contact_id": self.improve_demand_contact_id,
            "advertiserId": int(self.advertiser_id) if self.advertiser_id else None,
        }
        if self.agency_id:
            payload["agencyId"] = int(self.agency_id)
        return payload

    def _line_item_payload(
        self,
        package: MediaPackage,
        rate: float,
        rate_type: str,
        start_time: datetime,
        end_time: datetime,
    ) -> dict[str, Any]:
        """Build a ``CommonDealLineItemDto`` body for one package.

        Dry-run payload — surfaces what the adapter would send so operators
        can verify intent before live calls are validated (M2).
        """
        product_config = self._product_config_from_package(package)
        payload: dict[str, Any] = {
            "name": package.name or package.package_id,
            "start_date": start_time.date().isoformat(),
            "end_date": end_time.date().isoformat(),
            "time_zone": self.timezone,
            "currency": self.currency,
            "cpm_bid": rate,
            "pricing_model": product_config.get("pricing_model") or rate_type,
            "impression_cap": package.impressions,
            "reference_number": package.package_id,
        }
        for field in ("frequency_cap", "frequency_interval", "frequency_interval_type", "delivery_schedule"):
            if product_config.get(field) is not None:
                payload[field] = product_config[field]
        payload.update(build_targeting(package.targeting_overlay, product_config, tenant_id=self.tenant_id))
        return payload

    def _product_config_from_package(self, package: MediaPackage) -> dict[str, Any]:
        impl = getattr(package, "implementation_config", None) or {}
        return impl.get("improvedigital", impl) if isinstance(impl, dict) else {}

    def _buy_name(self, request: CreateMediaBuyRequest) -> str:
        """Derive a human-readable buy name — po_number when present,
        timestamp fallback so buys without one never collide."""
        if request.po_number:
            return f"adcp_{request.po_number}"
        return f"adcp_{int(datetime.now(UTC).timestamp())}"

    def _pending_creds_error(self, code: str = "pending_credentials") -> CreateMediaBuyError:
        return CreateMediaBuyError(errors=[Error(code=code, message=_PENDING_CREDS_MESSAGE, details=None)])

    # ----- creatives (Classic: hosted by the Improve adserver) -----

    def add_creative_assets(
        self, media_buy_id: str, assets: list[dict[str, Any]], today: datetime
    ) -> list[AssetStatus]:
        """POST each asset as a Classic creative on the buy's campaign.

        ``CreativeDto`` requires name/type/size/status/tag — assets missing
        the platform-required fields are rejected explicitly, never silently
        accepted as partial creatives.
        """
        campaign_id = media_buy_id.removeprefix("improvedigital_")
        statuses: list[AssetStatus] = []
        for asset in assets:
            creative_id = str(asset.get("creative_id") or asset.get("id") or "")
            payload = self._creative_payload(asset)
            if payload is None:
                statuses.append(
                    AssetStatus(
                        creative_id=creative_id,
                        status="failed",
                        message=(
                            "Improve Digital Classic creatives need a tag/URL and a size "
                            "(CreativeDto requires name, type, size, status, tag)"
                        ),
                    )
                )
                continue
            if self.dry_run:
                self.log(f"Would call: POST {self.base_url}/rtb/v1/classic/campaigns/{campaign_id}/creatives")
                self.log(f"  Creative: {payload}")
                statuses.append(AssetStatus(creative_id=creative_id, status="approved"))
            else:
                # Live mode — pending API credentials (blocker B2). Lands in M2.
                statuses.append(AssetStatus(creative_id=creative_id, status="failed", message=_PENDING_CREDS_MESSAGE))
        return statuses

    def _creative_payload(self, asset: dict[str, Any]) -> dict[str, Any] | None:
        """Map a canonical adapter asset onto a ``CreativeDto`` body.

        Returns ``None`` when the asset lacks the platform-required pieces
        (a servable tag/URL and a size).
        """
        tag = asset.get("snippet") or asset.get("tag") or asset.get("url")
        width, height = asset.get("width"), asset.get("height")
        if not tag or width is None or height is None:
            return None
        return {
            "name": asset.get("name") or asset.get("creative_id") or "adcp creative",
            "type": asset.get("asset_type") or "banner",
            "size": f"{int(width)}x{int(height)}",
            "status": "active",
            "tag": str(tag),
        }

    def associate_creatives(self, line_item_ids: list[str], platform_creative_ids: list[str]) -> list[dict[str, Any]]:
        """Bind Classic creatives to line items via
        ``PUT .../line-items/{id}/creatives``."""
        results: list[dict[str, Any]] = []
        for line_item_id in line_item_ids:
            for creative_id in platform_creative_ids:
                if self.dry_run:
                    self.log(f"Would call: PUT .../line-items/{line_item_id}/creatives (assign creative {creative_id})")
                    results.append({"line_item_id": line_item_id, "creative_id": creative_id, "status": "success"})
                else:
                    # Live mode — pending API credentials (blocker B2).
                    results.append(
                        {
                            "line_item_id": line_item_id,
                            "creative_id": creative_id,
                            "status": "failed",
                            "message": _PENDING_CREDS_MESSAGE,
                        }
                    )
        return results

    # ----- status / delivery -----

    def check_media_buy_status(self, media_buy_id: str, today: datetime) -> CheckMediaBuyStatusResponse:
        campaign_id = media_buy_id.removeprefix("improvedigital_")
        if self.dry_run:
            self.log(f"Would call: GET {self.base_url}/rtb/v1/classic/campaigns/{campaign_id}")
            return CheckMediaBuyStatusResponse(media_buy_id=media_buy_id, status="active")
        assert self._client is not None
        try:
            campaign = self._client.campaigns.get_campaign(int(campaign_id))
            status_value = str(campaign.get("status") or "active").lower()
            return CheckMediaBuyStatusResponse(media_buy_id=media_buy_id, status=status_value)
        except (ImproveDigitalError, ValueError) as exc:
            logger.warning("Improve Digital get_campaign failed: %s", exc)
            return CheckMediaBuyStatusResponse(media_buy_id=media_buy_id, status="unknown")

    def get_media_buy_delivery(
        self, media_buy_id: str, date_range: ReportingPeriod, today: datetime
    ) -> AdapterGetMediaBuyDeliveryResponse:
        if self.dry_run:
            return self._simulated_delivery_response(
                media_buy_id,
                date_range,
                today,
                target_impressions=500_000,
                cpm=4.0,
                completion_rate=0.7,
                currency=self.currency,
            )
        # Definitive metrics come from the Report API cache — Phase 3 / M3.
        # Raising (rather than returning zeros) tells the delivery webhook
        # scheduler to skip this buy instead of reporting false zeros.
        raise DeliveryDataUnavailable(
            media_buy_id,
            reason="Improve Digital reporting cache not implemented yet (Phase 3 of the integration plan)",
        )

    # ----- update_media_buy -----

    def update_media_buy(
        self,
        media_buy_id: str,
        action: str,
        package_id: str | None,
        budget: int | None,
        today: datetime,
    ) -> UpdateMediaBuyResponse:
        if action not in REQUIRED_UPDATE_ACTIONS:
            return self._unsupported_action_error(action)

        if self.dry_run:
            campaign_id = media_buy_id.removeprefix("improvedigital_")
            if action in {"pause_media_buy", "resume_media_buy"}:
                active = str(action == "resume_media_buy").lower()
                self.log(f"Would PUT .../campaigns/{campaign_id}/line-items/<each>/status?active={active}")
            elif action in {"pause_package", "resume_package"} and package_id:
                active = str(action == "resume_package").lower()
                self.log(f"Would PUT .../campaigns/{campaign_id}/line-items/{package_id}/status?active={active}")
            elif action in {"update_package_budget", "update_package_impressions"} and package_id and budget:
                field = "budget" if action == "update_package_budget" else "impression_cap"
                self.log(f"Would PUT /rtb/v1/classic/campaigns/{campaign_id}/line-items/{package_id} {field}={budget}")
            return UpdateMediaBuySuccess(media_buy_id=media_buy_id, affected_packages=[], implementation_date=today)

        # Live mode — pending API credentials (blocker B2). Lands in M2.
        from src.core.schemas import UpdateMediaBuyError

        return UpdateMediaBuyError(
            errors=[
                Error(
                    code="pending_credentials",
                    message="Improve Digital live-mode update_media_buy pending API credentials (blocker B2)",
                    details=None,
                )
            ]
        )
