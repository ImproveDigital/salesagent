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

Live coverage:

- ✅ dry-run for every tool path — echoes the Classic campaign / line-item /
  creative payloads the adapter would send.
- ✅ create_media_buy — Classic campaign + one line item per package +
  placement/package assignment; returns ``platform_line_item_id`` per
  package (persisted to ``media_packages.package_config`` by the core layer).
- ✅ add_creative_assets / associate_creatives — Classic creative CRUD +
  line-item binding.
- ✅ update_media_buy — pause/resume (buy + package), budget/impression
  updates, activate, archive; approval actions are platform no-ops
  (Classic campaigns have no approval workflow).
- ✅ check_media_buy_status / check_permissions.
- ✅ get_media_buy_delivery — aggregates the ``improvedigital_line_item_stats``
  cache populated by ``run_reporting_sync`` (Report API preview); raises
  ``DeliveryDataUnavailable`` while the cache is still empty.

Wire-shape notes (validated against the dev OpenAPI spec + live entities):
platform datetimes are ``YYYY-MM-DD HH:MM:SS`` strings; assignment
endpoints take ``{"line_item_placements"|"line_item_packages"|
"line_item_creatives": [{"id": ..., "assigned": true}]}`` envelopes;
status toggles are query-param PUTs.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime
from typing import Any

from adcp.types.aliases import Package as ResponsePackage

from src.adapters.base import (
    AdapterCapabilities,
    AdapterSyncResult,
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
    UpdateMediaBuyError,
    UpdateMediaBuyResponse,
    UpdateMediaBuySuccess,
)

logger = logging.getLogger(__name__)


def _domain_from_url(url: Any) -> str | None:
    """Bare hostname of a URL (``https://x.com/p`` → ``x.com``), or None."""
    if not url:
        return None
    from urllib.parse import urlparse

    host = urlparse(str(url)).netloc or str(url)
    host = host.split("@")[-1].split(":")[0]
    return host.removeprefix("www.") or None


class ImproveDigitalAdapter(AdServerAdapter):
    """AdCP adapter for Improve Digital Classic (direct) campaigns."""

    adapter_name = "improvedigital"
    default_channels = ["display", "olv", "audio", "native"]
    default_delivery_measurement = {"provider": "improvedigital"}
    connection_config_class = ImproveDigitalConnectionConfig
    product_config_class = ImproveDigitalProductConfig
    capabilities = AdapterCapabilities(
        supports_inventory_sync=True,
        supports_reporting_sync=True,
        inventory_entity_label="Placements",
        supports_custom_targeting=True,
        supports_geo_targeting=True,
        supports_dynamic_products=False,
        # CPM confirmed against live dev line items (pricing_model="CPM").
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
        # Optional — the Classic campaign API accepts campaigns without an
        # advertiser (every live dev campaign carries advertiserId=null).
        self.advertiser_id = self.principal.get_adapter_id("improvedigital") or self.config.get("default_advertiser_id")
        # Campaign identity chain (sandbox-confirmed): buying entity +
        # office are required on every campaign; the office carries a
        # default demand contact, so the explicit contact is an override.
        self.buying_entity_id = self.config.get("buying_entity_id")
        self.buying_entity_office_id = self.config.get("buying_entity_office_id")
        self.campaign_type = self.config.get("campaign_type") or "Improve"
        self.business_unit_id = self.config.get("business_unit_id")
        self.buyer_id = self.config.get("buyer_id")

        self.improve_demand_contact_id = self.config.get("improve_demand_contact_id")
        self.agency_id = self.config.get("agency_id")

        # Campaign-metadata attribution (CampaignMetadataDto). Tenant-level
        # for now, so every buy books under the same brand/agency/owners.
        # TODO(H2): make these per-buyer — resolve from the principal's
        # improvedigital platform mapping (and later the buyer-routing rules)
        # with this config as the fallback default, the same way GAM routes
        # buyer agents to advertisers.
        self.advertiser_uuid = self.config.get("advertiser_uuid")
        self.advertiser_name = self.config.get("advertiser_name")
        self.agency_name = self.config.get("agency_name")
        self.integration_platform_id = self.config.get("integration_platform_id")
        self.seat_id = self.config.get("seat_id")
        self.adops_person_id = self.config.get("adops_person_id")
        self.sales_person_id = self.config.get("sales_person_id")

        self.client_id = self.config.get("client_id")
        self.client_secret = self.config.get("client_secret")
        self.base_url = (self.config.get("api_base_url") or "https://api.360yield.com").rstrip("/")
        self.currency = self.config.get("currency", "EUR")
        self.timezone = self.config.get("timezone", "UTC")

        # Same-instance cache: line_item_id → campaign_id, populated by
        # create_media_buy so associate_creatives (called later in the same
        # request flow) can build the campaign-scoped binding URL without a
        # lookup. Cross-instance calls fall back to a DB scan.
        self._line_item_campaigns: dict[str, int] = {}

        if self.dry_run:
            self.log(
                "Running in dry-run mode — Improve Digital API calls will be simulated",
                dry_run_prefix=False,
            )
            self._client: ImproveDigitalClient | None = None
        else:
            # Only the OAuth pair is required to construct — reads (Test
            # Connection, inventory/reporting sync) work with credentials
            # alone. The buying-identity fields (demand contact, buying
            # entity, business unit) are validated at booking time in
            # create_media_buy, so a partially configured tenant can still
            # sync inventory while setup completes.
            if not (self.client_id and self.client_secret):
                raise ValueError("Improve Digital config requires client_id + client_secret")
            if not (self.buying_entity_id and self.buying_entity_office_id):
                raise ValueError(
                    "Improve Digital config requires buying_entity_id + buying_entity_office_id — "
                    "the Classic campaign API rejects campaigns without them (discover via "
                    "the adapter settings' Test Connection, or ask the Improve Digital team)"
                )
            self._client = ImproveDigitalClient(
                client_id=self.client_id,
                client_secret=self.client_secret,
                base_url=self.base_url,
                timeout=90.0,
            )

    def _missing_booking_config(self) -> list[str]:
        """Connection-config fields the Classic booking APIs require but
        reads do not — checked at create time, not construction time."""
        missing = []
        if not self.improve_demand_contact_id:
            missing.append("improve_demand_contact_id (your API user's user_id — see /lookup/v1/user-details)")
        if not self.buying_entity_id:
            missing.append("buying_entity_id (e.g. 421 = Improve Digital Marketplace on dev)")
        if not self.business_unit_id:
            missing.append("business_unit_id (e.g. 33 = Azerion on dev)")
        return missing

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

    # ----- inventory sync -----

    def run_inventory_sync(self) -> AdapterSyncResult:
        """Refresh the local 360Yield inventory cache (placements,
        publishers, packages, sizes) from the API.

        Called by the shared adapter sync scheduler and the admin
        "Sync Inventory Now" button.
        """
        start = datetime.now(UTC)
        if self.dry_run or self._client is None:
            return AdapterSyncResult(
                sync_kind="inventory",
                started_at=start,
                finished_at=datetime.now(UTC),
                succeeded=False,
                errors={"adapter": "dry-run mode — Improve Digital inventory sync requires live credentials"},
            )

        from src.adapters.improvedigital.inventory_sync import ImproveDigitalInventorySync
        from src.core.database.database_session import get_db_session

        with get_db_session() as session:
            sync = ImproveDigitalInventorySync(
                client=self._client, session=session, tenant_id=self.tenant_id or "default"
            )
            inner = sync.run()
            session.commit()

        return AdapterSyncResult(
            sync_kind="inventory",
            started_at=inner.started_at or start,
            finished_at=inner.finished_at or datetime.now(UTC),
            succeeded=inner.succeeded,
            counts=dict(inner.counts),
            errors=dict(inner.errors),
        )

    def latest_inventory_sync_at(self) -> datetime | None:
        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository

        with get_db_session() as session:
            return ImproveDigitalInventoryRepository(session, self.tenant_id or "default").latest_sync_at()

    async def get_available_inventory(self) -> dict[str, Any]:
        """Surface the synced placement cache to the AI product configurator."""
        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository

        with get_db_session() as session:
            repo = ImproveDigitalInventoryRepository(session, self.tenant_id or "default")
            placements = [
                {"id": row.entity_id, "name": row.name, "publisher_id": row.parent_id}
                for row in repo.list_by_type("placement")
            ]
            packages = [{"id": row.entity_id, "name": row.name} for row in repo.list_by_type("package")]
            sizes = [{"id": row.entity_id, "name": row.name} for row in repo.list_by_type("size")]
            publishers = [{"id": row.entity_id, "name": row.name} for row in repo.list_by_type("publisher")]

        return {
            "placements": placements,
            "ad_units": [],
            "targeting_options": {"packages": packages, "sizes": sizes, "publishers": publishers},
            "creative_specs": sizes,
            "properties": {
                "placement_count": len(placements),
                "publisher_count": len(publishers),
                "package_count": len(packages),
                "size_count": len(sizes),
            },
        }

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
            if self._has_campaign_metadata():
                self.log(f"Would call: POST {self.base_url}/api/metadata-campaigns")
                self.log(f"  Metadata: {self._campaign_metadata_payload(0, buy_name, start_time, end_time)}")
            for package in packages:
                rate, rate_type = self._resolve_pricing_rate(package, package_pricing_info)
                payload = self._line_item_payload(package, rate, rate_type, start_time, end_time)
                self.log(f"Would call: POST {self.base_url}/rtb/v1/classic/campaigns/<new>/line-items")
                self.log(f"  LineItem: {payload}")
                product_config = self._product_config_from_package(package)
                if product_config.get("placement_ids") or product_config.get("package_ids"):
                    self.log(
                        "Would call: PUT .../line-items/<new>/placements + .../packages "
                        f"placement_ids={product_config.get('placement_ids', [])} "
                        f"package_ids={product_config.get('package_ids', [])}"
                    )
            return self._build_create_success(request, f"improvedigital_{buy_name}", packages)

        # Live mode — Classic campaign > line items (one per package) >
        # placement/package assignment.
        assert self._client is not None
        missing = self._missing_booking_config()
        if missing:
            return CreateMediaBuyError(
                errors=[
                    Error(
                        code="incomplete_adapter_config",
                        message=(
                            "Improve Digital booking needs connection-config fields that are not set: "
                            + "; ".join(missing)
                            + ". Fill them in the tenant's adapter settings page and retry."
                        ),
                        details=None,
                    )
                ]
            )
        campaign_id: int | None = None
        try:
            campaign = self._client.campaigns.create_campaign(self._campaign_payload(buy_name, start_time, end_time))
            campaign_id = int(campaign["id"])
            # Commercial attribution rides on its own record, posted before
            # the line items so a rejection cleans up the campaign alone.
            if self._has_campaign_metadata():
                self._client.metadata.upsert_campaign_metadata(
                    self._campaign_metadata_payload(campaign_id, buy_name, start_time, end_time)
                )
                logger.info("Improve Digital: campaign metadata attached for campaign %s", campaign_id)
            else:
                logger.info(
                    "Improve Digital: campaign metadata skipped for campaign %s — no attribution "
                    "fields configured for tenant %s (fill them in the adapter settings page)",
                    campaign_id,
                    self.tenant_id,
                )
            platform_line_item_ids: dict[str, str] = {}
            package_responses: list[ResponsePackage] = []
            for package in packages:
                rate, rate_type = self._resolve_pricing_rate(package, package_pricing_info)
                payload = self._line_item_payload(package, rate, rate_type, start_time, end_time)
                # Geo travels via its own per-line-item endpoint, not the
                # create body (LineItemGeoTargetingDto — see targeting.py).
                geo_targeting = payload.pop("geo_targeting", None)
                line_item = self._client.campaigns.create_line_item(campaign_id, payload)
                line_item_id = int(line_item["id"])
                self._line_item_campaigns[str(line_item_id)] = campaign_id
                self._assign_inventory(campaign_id, line_item_id, package)
                if geo_targeting:
                    self._client.campaigns.set_line_item_geo_targeting(
                        campaign_id,
                        line_item_id,
                        {"filter": True, "geo_targeting": self._resolve_geo_regions(geo_targeting)},
                    )
                platform_line_item_ids[package.package_id] = str(line_item_id)
                package_responses.append(
                    ResponsePackage(
                        package_id=package.package_id,
                        paused=False,
                        platform_line_item_id=str(line_item_id),
                    )
                )
        except (ImproveDigitalError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Improve Digital create_media_buy failed: %s", exc)
            self._cleanup_partial_campaign(campaign_id)
            return CreateMediaBuyError(
                errors=[
                    Error(
                        code="upstream_error",
                        message=f"Improve Digital rejected the request: {exc}",
                        details=None,
                    )
                ]
            )

        response = self._build_create_success(
            request,
            f"improvedigital_{campaign_id}",
            packages,
            package_responses=package_responses,
        )
        # The core layer persists this mapping into
        # media_packages.package_config["platform_line_item_id"] — required
        # by update_media_buy and the reporting read path.
        object.__setattr__(response, "_platform_line_item_ids", platform_line_item_ids)
        return response

    def _cleanup_partial_campaign(self, campaign_id: int | None) -> None:
        """Best-effort removal of a campaign whose packages failed mid-create,
        so aborted buys don't leave orphan campaigns on the platform. Delete
        is refused once the campaign has served impressions — archive then."""
        if campaign_id is None or self._client is None:
            return
        try:
            self._client.campaigns.delete_campaign(campaign_id)
        except ImproveDigitalError:
            try:
                self._client.campaigns.archive_campaign(campaign_id)
            except ImproveDigitalError:
                logger.warning("Improve Digital: could not clean up partial campaign %s", campaign_id)

    def _assign_inventory(self, campaign_id: int, line_item_id: int, package: MediaPackage) -> None:
        """Assign the product's placements/packages to a freshly created
        line item. A line item with neither selection serves nowhere, so a
        missing selection is a loud product-config error, not a silent pass."""
        assert self._client is not None
        product_config = self._product_config_from_package(package)
        placement_ids = product_config.get("placement_ids") or []
        package_ids = product_config.get("package_ids") or []
        if not placement_ids and not package_ids:
            raise ValueError(
                f"Package {package.package_id!r} has no placement_ids or package_ids in its "
                "improvedigital product config — the Classic line item would target no inventory"
            )
        if placement_ids:
            self._client.campaigns.set_line_item_placements(
                campaign_id,
                line_item_id,
                {"line_item_placements": [{"id": int(pid), "assigned": True} for pid in placement_ids]},
            )
        if package_ids:
            self._client.campaigns.set_packages(
                campaign_id,
                line_item_id,
                {"line_item_packages": [{"id": int(pid), "assigned": True} for pid in package_ids]},
            )

    def _resolve_geo_regions(self, geo_targeting: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Complete geo entries to the platform's required shape.

        The live geo-targeting endpoint requires ``exclude`` AND ``region``
        on every entry (validated live: HTTP 400 'object has missing
        required properties ["exclude","region"]'), and geo values must be
        the platform's own display names. Country tokens are resolved
        against the platform geo dictionary — accepting ISO alpha-2 codes
        (all AdCP buyer overlays: ``GeoCountry`` is ``^[A-Z]{2}$``), CLDR
        English names, and case/diacritic variants — and rewritten to the
        platform's exact spelling with their region attached. Region tokens
        are matched against the platform region list the same way. An
        unresolvable token fails the booking loudly — sending it would 400
        upstream anyway.
        """
        from src.adapters.improvedigital.targeting import candidate_country_names, normalize_geo_name

        countries, regions = self._geo_dictionary()
        resolved = []
        for entry in geo_targeting:
            entry = dict(entry)
            entry.setdefault("exclude", False)
            if entry.get("country"):
                match = next(
                    (
                        countries[normalize_geo_name(candidate)]
                        for candidate in candidate_country_names(str(entry["country"]))
                        if normalize_geo_name(candidate) in countries
                    ),
                    None,
                )
                if match is None:
                    raise ValueError(
                        f"could not resolve country {entry['country']!r} in the 360Yield geo "
                        "dictionary — use an ISO alpha-2 code or a platform country name "
                        "(pick them from the product page dropdowns)"
                    )
                entry["country"], entry["region"] = match
            elif entry.get("region"):
                region_name = regions.get(normalize_geo_name(str(entry["region"])))
                if region_name is None:
                    raise ValueError(
                        f"could not resolve region {entry['region']!r} in the 360Yield geo "
                        f"dictionary — valid regions: {', '.join(sorted(regions.values()))}"
                    )
                entry["region"] = region_name
            resolved.append(entry)

        # Post-resolution dedup: distinct input tokens ('NL' from the buyer,
        # 'The Netherlands' from the product config) resolve to identical
        # rows — send each geo once. When include and exclude collide on the
        # same geo, the exclude wins: AdCP overlay semantics are that the
        # buyer's exclusion narrows the product default, and sending both
        # contradictory rows would delegate the outcome to undocumented
        # platform precedence.
        deduped: dict[tuple[str | None, str | None], dict[str, Any]] = {}
        for entry in resolved:
            key = (entry.get("country"), entry.get("region"))
            existing = deduped.get(key)
            if existing is None or (entry["exclude"] and not existing["exclude"]):
                deduped[key] = entry
        return list(deduped.values())

    def _geo_dictionary(self) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
        """The platform geo dictionary, keyed by normalized display name.

        Returns ``(countries, regions)`` where ``countries`` maps a
        normalized country name to ``(platform_country_name, region_name)``
        and ``regions`` maps a normalized region name to the platform
        region name. Built from ``/rtb/v1/regions`` +
        ``/rtb/v1/regions/{name}/countries`` (both paginated), once per
        adapter instance.
        """
        from src.adapters.improvedigital.targeting import normalize_geo_name

        assert self._client is not None
        if not hasattr(self, "_geo_dictionary_cache"):
            client = self._client
            countries: dict[str, tuple[str, str]] = {}
            regions: dict[str, str] = {}
            for region_name in self._paginated_geo_names(client.lookups.regions, "regions"):
                regions.setdefault(normalize_geo_name(region_name), region_name)
                for country_name in self._paginated_geo_names(
                    lambda **params: client.lookups.region_countries(region_name, **params),  # noqa: B023
                    "countries",
                ):
                    countries.setdefault(normalize_geo_name(country_name), (country_name, region_name))
            self._geo_dictionary_cache: tuple[dict[str, tuple[str, str]], dict[str, str]] = (countries, regions)
        return self._geo_dictionary_cache

    @staticmethod
    def _paginated_geo_names(fetch: Any, envelope_key: str) -> list[str]:
        """Exhaust an offset/limit-paginated geo dictionary endpoint and
        return the row names. Stops on an empty page, a page with no unseen
        names (server ignoring ``offset``), or a 10k backstop."""
        names: list[str] = []
        seen: set[str] = set()
        offset = 0
        while offset <= 10_000:
            body = fetch(limit=100, offset=offset)
            rows = body.get(envelope_key) if isinstance(body, dict) else body
            rows = rows if isinstance(rows, list) else []
            if not rows:
                break
            page_names = [
                str(row.get("name")) if isinstance(row, dict) else str(row)
                for row in rows
                if (isinstance(row, dict) and row.get("name")) or not isinstance(row, dict)
            ]
            fresh = [name for name in page_names if name not in seen]
            if not fresh:
                break
            seen.update(fresh)
            names.extend(fresh)
            offset += len(rows)
        return names

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        """Platform datetime wire format (``YYYY-MM-DD HH:MM:SS``) —
        confirmed against live dev campaigns/line items."""
        return value.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _format_iso_datetime(value: datetime) -> str:
        """Metadata API datetime format — ISO-8601 UTC with milliseconds
        (``2026-08-14T14:45:18.407Z``). Naive values are read as UTC rather
        than the host's local zone, which would silently shift the flight."""
        moment = (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)
        return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"

    def _campaign_payload(self, buy_name: str, start_time: datetime, end_time: datetime) -> dict[str, Any]:
        """Build a ``CampaignDto`` body.

        The Classic create schema requires (validated live on the dev
        platform): name, start_date, time_zone, type, buying_entity_id —
        plus improve_demand_contact_id per the platform docs. Classic
        (direct) campaigns carry ``type: "Improve"`` (the Improve adserver
        hosts and serves the creatives).
        """
        payload: dict[str, Any] = {
            "name": buy_name,
            "type": "Improve",
            "start_date": self._format_datetime(start_time),
            "end_date": self._format_datetime(end_time),
            "time_zone": self.timezone,
            "currency": self.currency,
            "improve_demand_contact_id": self.improve_demand_contact_id,
            "buying_entity_id": int(self.buying_entity_id) if self.buying_entity_id else None,
            # Numeric platform advertiser IDs only — principal mappings may
            # carry metadata-advertiser UUIDs, which CampaignDto.advertiserId
            # (integer) cannot hold.
            "advertiserId": (int(str(self.advertiser_id)) if str(self.advertiser_id or "").isdigit() else None),
        }
        if self.buying_entity_office_id:
            payload["buying_entity_office_id"] = int(self.buying_entity_office_id)
            payload["buying_entity_office_ids"] = [int(self.buying_entity_office_id)]
        if self.agency_id:
            payload["agencyId"] = int(self.agency_id)
        return payload

    def _has_campaign_metadata(self) -> bool:
        """Whether this tenant configured any campaign-metadata attribution.

        Metadata is required for production bookings but not for the
        Classic API itself, so an unconfigured tenant (dev, smoke tests)
        books without it rather than failing.
        """
        return any(
            (
                self.advertiser_uuid,
                self.agency_id,
                self.adops_person_id,
                self.sales_person_id,
                self.integration_platform_id,
            )
        )

    def _campaign_metadata_payload(
        self, campaign_id: int, buy_name: str, start_time: datetime, end_time: datetime
    ) -> dict[str, Any]:
        """Build the ``CampaignMetadataDto`` for a freshly created campaign.

        The metadata API is a surface parallel to booking: the record is
        keyed by ``campaignId`` and carries the commercial attribution
        (brand, agency, owners, DSP seat) that the Classic ``CampaignDto``
        has no room for. Only ``campaignId`` is schema-required; everything
        else comes from tenant config and is omitted when unset.

        Wire notes: the DTO is camelCase where booking is snake_case, and
        its dates are ISO-8601 UTC strings (``2026-08-14T14:45:18.407Z``)
        where booking uses ``YYYY-MM-DD HH:MM:SS`` in the campaign's own
        timezone. ``campaignId`` is a string here even though the Classic
        campaign id is an integer.
        ``entityType``/``completed``/``isCompleted`` are fixed values pending
        confirmation of what else the platform derives.
        """
        payload: dict[str, Any] = {
            "campaignId": str(campaign_id),
            "campaignName": buy_name,
            "campaignStartDate": self._format_iso_datetime(start_time),
            "campaignEndDate": self._format_iso_datetime(end_time),
            "currencyCode": self.currency,
            "entityType": "c",
            "completed": True,
            "isCompleted": True,
        }
        optional: dict[str, Any] = {
            "advertiserUuid": self.advertiser_uuid,
            "advertiserName": self.advertiser_name,
            "agencyId": int(self.agency_id) if self.agency_id else None,
            "agencyName": self.agency_name,
            "businessUnitId": int(self.business_unit_id) if self.business_unit_id else None,
            "buyerId": int(self.buyer_id) if self.buyer_id else None,
            "integrationPlatformId": (int(self.integration_platform_id) if self.integration_platform_id else None),
            "seatId": self.seat_id,
            "adOpsPersonId": int(self.adops_person_id) if self.adops_person_id else None,
            "salesPersonId": self.sales_person_id,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
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

        Required by the Classic line-item API (validated live on the dev
        platform): name, type, line_item_status, start_date,
        business_unit_id, improve_demand_contact_id, and a goal for CPM
        line items.

        The booking goal comes from the product's ``goal`` config: ``BUDGET``
        (the default) paces against the money figure, ``IMPRESSION`` against
        the impression cap. Both figures ride along either way — on the line
        item itself and on a single ``flight_details`` row spanning the
        flight — because the platform's own create sends both. The
        delivery/compliance flags below mirror that same payload: the API
        leaves them unset rather than defaulted when omitted.
        """
        product_config = self._with_product_geo_defaults(package, self._product_config_from_package(package))
        goal = str(product_config.get("goal") or "BUDGET").upper()
        # Packages booked from a buy-level budget carry no per-package figure;
        # reverse the core layer's budget→goal-units conversion
        # (media_buy_create._goal_units_from_budget) so a BUDGET-goal line item
        # always books the money the impression cap was sized for.
        budget = float(package.budget) if package.budget is not None else round(package.impressions * rate / 1000, 2)
        payload: dict[str, Any] = {
            "name": package.name or package.package_id,
            "type": "Standard",
            "line_item_status": "Inactive",
            "goal": goal,
            "start_date": self._format_datetime(start_time),
            "end_date": self._format_datetime(end_time),
            "time_zone": self.timezone,
            # No currency on line items — the platform requires it to match
            # the campaign owner's default and inherits it when omitted
            # (validated live: sending it 400s with "Currency field does not
            # match default campaign owner/publisher currency").
            "cpm_bid": rate,
            "pricing_model": product_config.get("pricing_model") or rate_type,
            "pricing_model_type": "First Bid",
            "impression_cap": package.impressions,
            "impression_cap_daily": False,
            "budget_is_daily": False,
            "invoice_type": "on_actuals",
            "delivery_schedule": product_config.get("delivery_schedule") or "Evenly",
            "third_party_inventory": True,
            "is_optimised": True,
            "is_dynamic_optimization": False,
            "dynamic_optimization_kpi_type": "",
            "dynamic_optimization_kpi_value": 0,
            "conversion_tracking_enabled": False,
            "keep_on_delivering": False,
            "track_viewability": False,
            "is_consentless": False,
            "is_coppa_compliant": False,
            "optout_mechanism": [],
            "reference_number": package.package_id,
            "improve_demand_contact_id": self.improve_demand_contact_id,
        }
        payload["budget"] = budget
        payload["flight_details"] = [self._flight_detail(budget, package.impressions, start_time, end_time)]
        if self.business_unit_id:
            payload["business_unit_id"] = int(self.business_unit_id)
        if self.buyer_id:
            payload["buyer_id"] = int(self.buyer_id)
        for field in ("frequency_cap", "frequency_interval", "frequency_interval_type"):
            if product_config.get(field) is not None:
                payload[field] = product_config[field]
        payload.update(build_targeting(package.targeting_overlay, product_config, tenant_id=self.tenant_id))
        return payload

    def _flight_detail(
        self, budget: float, impressions: int, start_time: datetime, end_time: datetime
    ) -> dict[str, Any]:
        """One flight row covering the whole booking window.

        The platform splits a line item's pacing into flights; AdCP buys have
        a single flight, so the row mirrors the line item's own window, budget
        and impression cap.
        """
        return {
            "start_time": self._format_datetime(start_time),
            "end_time": self._format_datetime(end_time),
            "budget": budget,
            "budget_is_daily": False,
            "impression_cap": impressions,
            "impression_cap_daily": False,
        }

    def _product_config_from_package(self, package: MediaPackage) -> dict[str, Any]:
        impl = getattr(package, "implementation_config", None) or {}
        return impl.get("improvedigital", impl) if isinstance(impl, dict) else {}

    def _with_product_geo_defaults(self, package: MediaPackage, product_config: dict[str, Any]) -> dict[str, Any]:
        """Fold the product's generic "Target Countries" selection
        (``Product.countries``, ISO alpha-2 codes from the Channel &
        Geographic Targeting section) into the adapter geo defaults, so the
        product page's advertised geo is what the booked line items actually
        target.

        The visible generic selection wins over any legacy
        ``geo_countries`` stored in the adapter config; both lose to
        nothing — an empty selection means no geo restriction. Live-only
        (dry-run must stay DB-free).
        """
        if self.dry_run or not getattr(package, "product_id", None):
            return product_config

        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.product import ProductRepository

        with get_db_session() as session:
            product = ProductRepository(session, self.tenant_id or "default").get_by_id(str(package.product_id))
            countries = list(product.countries) if product is not None and product.countries else None
        if not countries:
            return product_config
        return {**product_config, "geo_countries": countries}

    def _buy_name(self, request: CreateMediaBuyRequest) -> str:
        """Derive a human-readable buy name — po_number when present,
        timestamp fallback so buys without one never collide."""
        if request.po_number:
            return f"adcp_{request.po_number}"
        return f"adcp_{int(datetime.now(UTC).timestamp())}"

    # ----- creatives (Classic: hosted by the Improve adserver) -----

    def add_creative_assets(
        self, media_buy_id: str, assets: list[dict[str, Any]], today: datetime
    ) -> list[AssetStatus]:
        """POST each asset as a Classic creative on the buy's campaign.

        ``CreativeDto`` requires name/type/size/status/tag/advertiser_domain
        — assets missing the platform-required fields are rejected
        explicitly, never silently accepted as partial creatives.
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
                            "Improve Digital Classic creatives need a tag/URL, a size, and an "
                            "advertiser domain (CreativeDto requires name, type, size, status, "
                            "tag, advertiser_domain)"
                        ),
                    )
                )
                continue
            if self.dry_run:
                self.log(f"Would call: POST {self.base_url}/rtb/v1/classic/campaigns/{campaign_id}/creatives")
                self.log(f"  Creative: {payload}")
                statuses.append(AssetStatus(creative_id=creative_id, status="approved"))
                continue
            assert self._client is not None
            try:
                created = self._create_live_creative(int(campaign_id), payload)
                # Echo the platform creative ID — the core layer stores it as
                # platform_creative_id and passes it back to associate_creatives.
                statuses.append(AssetStatus(creative_id=str(created["id"]), status="approved"))
            except (ImproveDigitalError, KeyError, IndexError, TypeError, ValueError) as exc:
                logger.warning("Improve Digital create_creative failed for %s: %s", creative_id, exc)
                statuses.append(
                    AssetStatus(
                        creative_id=creative_id,
                        status="failed",
                        message=f"Improve Digital rejected the creative: {exc}",
                    )
                )
        return statuses

    def _create_live_creative(self, campaign_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Create one Classic creative and return the created entity.

        Tag-based creatives go through the plain-JSON bulk-upload endpoint
        (validated live); other types use the multipart single-create
        servlet. Both paths need the platform ``size_id`` — resolved from
        ``GET /rtb/v1/sizes-all`` by the creative's pixel dimensions.
        """
        assert self._client is not None
        size = self._resolve_size(int(payload["width"]), int(payload["height"]))
        if size is not None:
            payload = {**payload, "size": size["name"], "size_id": size["id"]}
        if payload.get("type") == "Third Party Tag":
            body = {k: v for k, v in payload.items() if k != "type"}
            created = self._client.creatives.create_third_party_tag_creatives(campaign_id, [body])
        else:
            created = self._client.creatives.create_creative(campaign_id, payload)
        creative_id = self._created_creative_id(created)
        if creative_id is None:
            # The platform accepted the create but the response carried no
            # usable ID (endpoint response shapes vary between the bulk
            # servlets). Without the ID the creative would exist upstream but
            # never get bound to a line item — recover it by name from the
            # campaign's creative list.
            creative_id = self._find_creative_id_by_name(campaign_id, str(payload.get("name") or ""))
        if creative_id is None:
            raise ValueError(
                f"could not determine the platform creative ID (create response: {str(created)[:200]!r}); "
                "the creative may exist on the platform but cannot be bound to a line item"
            )
        return {"id": creative_id}

    @staticmethod
    def _created_creative_id(created: Any) -> int | None:
        """Extract the platform creative ID from a create response.

        Handles the shapes seen across the Classic creative endpoints: a bare
        ``CreativeDto`` list (per the OpenAPI spec), an enveloped list
        (``{"creatives"|"content"|"data": [...]}``), or a single entity dict.
        Returns ``None`` when no row carries an ID (e.g. empty 200 body).
        """
        rows: Any = created
        if isinstance(created, dict):
            for key in ("creatives", "content", "data"):
                if isinstance(created.get(key), list):
                    rows = created[key]
                    break
            else:
                rows = [created]
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    for id_key in ("id", "creative_id"):
                        if row.get(id_key) is not None:
                            return int(row[id_key])
        return None

    def _find_creative_id_by_name(self, campaign_id: int, name: str) -> int | None:
        """Recover a just-created creative's platform ID by name lookup.

        Newest ID wins when names repeat — the creative created moments ago
        is by construction the highest ID for that name."""
        if not name:
            return None
        assert self._client is not None
        try:
            body = self._client.creatives.list_creatives(campaign_id)
        except ImproveDigitalError as exc:
            logger.warning("Improve Digital creative recovery lookup failed for campaign %s: %s", campaign_id, exc)
            return None
        rows = body.get("creatives") if isinstance(body, dict) else body
        matches = [
            row
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and row.get("name") == name and str(row.get("id") or "").isdigit()
        ]
        if not matches:
            return None
        return max(int(row["id"]) for row in matches)

    def _resolve_size(self, width: int, height: int) -> dict[str, Any] | None:
        """Look up the platform size entry for a width×height pair.

        The creative endpoints resolve sizes by display name/ID — a bare
        ``300x250`` string is rejected with "no available placements with
        this creative size" (verified live)."""
        assert self._client is not None
        if not hasattr(self, "_sizes_cache"):
            body = self._client.lookups.sizes()
            rows = body.get("sizes") if isinstance(body, dict) else body
            self._sizes_cache: list[dict[str, Any]] = rows if isinstance(rows, list) else []
        for row in self._sizes_cache:
            if row.get("width") == width and row.get("height") == height:
                return row
        return None

    def _creative_payload(self, asset: dict[str, Any]) -> dict[str, Any] | None:
        """Map a canonical adapter asset onto a ``CreativeDto`` body.

        Returns ``None`` when the asset lacks the platform-required pieces
        (a servable tag/URL, a size, and an advertiser domain — the create
        schema requires name/type/size/status/tag/advertiser_domain,
        validated live on the dev platform).
        """
        width, height = asset.get("width"), asset.get("height")
        advertiser_domain = asset.get("advertiser_domain") or _domain_from_url(
            asset.get("click_url") or asset.get("click_through_url") or asset.get("landing_page_url")
        )
        creative_type = self._creative_type(asset)
        tag = asset.get("snippet") or asset.get("tag") or asset.get("url")
        if creative_type == "Third Party Tag" and not (asset.get("snippet") or asset.get("tag")):
            # The bulk-upload endpoint validates the tag is real HTML
            # (<html>|<script>|<img>|<ins>|<div>|<iframe>|<a>) — a bare
            # asset URL is rejected with creative.tag.valid (verified live).
            # Hosted-image assets get a synthesized <img> tag instead.
            tag = self._image_tag(asset)
        if not tag or width is None or height is None or not advertiser_domain:
            return None
        payload: dict[str, Any] = {
            "name": asset.get("name") or asset.get("creative_id") or "adcp creative",
            "type": creative_type,
            "size": f"{int(width)}x{int(height)}",
            "width": int(width),
            "height": int(height),
            "status": "Active",
            "tag": str(tag),
            "advertiser_domain": advertiser_domain,
        }
        if creative_type == "Third Party Tag":
            # Required alongside the tag (validated live): the served
            # medium and the platforms the tag may render on.
            payload["third_party_type"] = "display"
            payload["platform_types"] = ["Web"]
            payload["tag_secure"] = True
        return payload

    @staticmethod
    def _image_tag(asset: dict[str, Any]) -> str | None:
        """Wrap a hosted-image URL in servable HTML for the tag endpoint."""
        url = asset.get("url")
        width, height = asset.get("width"), asset.get("height")
        if not url or width is None or height is None:
            return None
        img = (
            f'<img src="{html.escape(str(url), quote=True)}" '
            f'width="{int(width)}" height="{int(height)}" style="border:0;" alt="">'
        )
        click = asset.get("click_url") or asset.get("click_through_url") or asset.get("landing_page_url")
        if click:
            return f'<a href="{html.escape(str(click), quote=True)}" target="_blank">{img}</a>'
        return img

    @staticmethod
    def _creative_type(asset: dict[str, Any]) -> str:
        """Map a canonical asset type onto the platform's CreativeType name.

        Canonical names come from ``GET /common/v1/i18n/creative_type``:
        Third Party Tag, Image (PNG, GIF, JPG), Html5, VAST, VAST_VPAID,
        Video File, Native, Ad Builder, Raw Video, VAST Audio.
        """
        asset_type = str(asset.get("asset_type") or "").lower()
        if "audio" in asset_type:
            return "VAST Audio"
        if "video" in asset_type or "vast" in asset_type:
            return "VAST"
        if "native" in asset_type:
            return "Native"
        # Tag/snippet-based display creatives (banner, html, rich media).
        return "Third Party Tag"

    def associate_creatives(self, line_item_ids: list[str], platform_creative_ids: list[str]) -> list[dict[str, Any]]:
        """Bind Classic creatives to line items via
        ``PUT .../line-items/{id}/creatives`` (one call per line item,
        assigning every creative)."""
        results: list[dict[str, Any]] = []
        for line_item_id in line_item_ids:
            if self.dry_run:
                for creative_id in platform_creative_ids:
                    self.log(f"Would call: PUT .../line-items/{line_item_id}/creatives (assign creative {creative_id})")
                    results.append({"line_item_id": line_item_id, "creative_id": creative_id, "status": "success"})
                continue
            assert self._client is not None
            campaign_id = self._campaign_id_for_line_item(line_item_id)
            if campaign_id is None:
                results.extend(
                    {
                        "line_item_id": line_item_id,
                        "creative_id": creative_id,
                        "status": "failed",
                        "message": f"No Classic campaign found for line item {line_item_id}",
                    }
                    for creative_id in platform_creative_ids
                )
                continue
            try:
                self._client.creatives.set_line_item_creatives(
                    campaign_id,
                    int(line_item_id),
                    {"line_item_creatives": [{"id": int(cid), "assigned": True} for cid in platform_creative_ids]},
                )
                results.extend(
                    {"line_item_id": line_item_id, "creative_id": creative_id, "status": "success"}
                    for creative_id in platform_creative_ids
                )
            except (ImproveDigitalError, ValueError) as exc:
                logger.warning("Improve Digital creative binding failed for line item %s: %s", line_item_id, exc)
                results.extend(
                    {
                        "line_item_id": line_item_id,
                        "creative_id": creative_id,
                        "status": "failed",
                        "message": str(exc),
                    }
                    for creative_id in platform_creative_ids
                )
        return results

    def _campaign_id_for_line_item(self, line_item_id: str) -> int | None:
        """Resolve the Classic campaign owning a line item.

        The creative-binding endpoint is campaign-scoped. Within the create
        flow the mapping comes from the same-instance cache; otherwise it is
        recovered from ``media_packages.package_config`` (the core layer
        persists ``platform_line_item_id`` per package on create).
        """
        cached = self._line_item_campaigns.get(str(line_item_id))
        if cached is not None:
            return cached

        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.media_buy import MediaBuyRepository

        with get_db_session() as session:
            repo = MediaBuyRepository(session, self.tenant_id or "default")
            # No status filter — creative binding must also resolve buys that
            # haven't started serving yet (pending_start, HITL states).
            buy = repo.find_by_platform_line_item_id(str(line_item_id))
            if buy is not None:
                for buy_ref in (buy.external_id, buy.media_buy_id):
                    campaign_ref = str(buy_ref or "").removeprefix("improvedigital_")
                    if campaign_ref.isdigit():
                        campaign_id = int(campaign_ref)
                        self._line_item_campaigns[str(line_item_id)] = campaign_id
                        return campaign_id
        return None

    def _resolve_campaign_id(self, media_buy_id: str) -> str:
        """Classic campaign ID behind a media buy reference.

        The adapter returns ``improvedigital_<campaign_id>`` at create time,
        but delivery callers (the admin detail page, the MCP delivery tool)
        pass the core layer's internal ID (``mb_*``) — the adapter reference
        is stamped on ``media_buys.external_id``. Mirror the reporting
        sync's write-side resolution so read paths hit the same cache rows.
        """
        campaign_id = media_buy_id.removeprefix("improvedigital_")
        if campaign_id.isdigit():
            return campaign_id

        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.media_buy import MediaBuyRepository

        with get_db_session() as session:
            repo = MediaBuyRepository(session, self.tenant_id or "default")
            buy = repo.get_by_id(media_buy_id)
            external = str(buy.external_id or "") if buy is not None else ""
        external_campaign = external.removeprefix("improvedigital_")
        if external_campaign.isdigit():
            return external_campaign
        return campaign_id

    # ----- status / delivery -----

    def check_media_buy_status(self, media_buy_id: str, today: datetime) -> CheckMediaBuyStatusResponse:
        campaign_id = (
            media_buy_id.removeprefix("improvedigital_") if self.dry_run else self._resolve_campaign_id(media_buy_id)
        )
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
        # Definitive metrics come from the Report API cache populated by
        # run_reporting_sync. Raising (rather than returning zeros) while the
        # cache is empty tells the delivery webhook scheduler to skip this
        # buy instead of reporting false zeros.
        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.improvedigital_line_item_stats import (
            ImproveDigitalLineItemStatsRepository,
        )

        campaign_id = self._resolve_campaign_id(media_buy_id)
        with get_db_session() as session:
            repo = ImproveDigitalLineItemStatsRepository(session, self.tenant_id or "default")
            stat_rows = repo.list_by_campaign(campaign_id)
        if not stat_rows:
            raise DeliveryDataUnavailable(
                media_buy_id,
                reason="Improve Digital reporting cache has no rows for this buy yet (reporting sync pending)",
            )
        return self._aggregate_stat_rows_to_delivery_response(
            media_buy_id,
            date_range,
            stat_rows,
            package_id_attr="line_item_id",
            default_currency=self.currency,
        )

    # ----- reporting sync -----

    def run_reporting_sync(self) -> AdapterSyncResult:
        """Refresh the ``improvedigital_line_item_stats`` cache from the
        Report API (preview, ≤500 rows) for this tenant's active buys.

        Called by the shared adapter reporting-sync scheduler and the admin
        "Sync Reporting Now" button.
        """
        from src.adapters.improvedigital.reporting_sync import (
            ImproveDigitalReportingSync,
            ReportingScopeNotGranted,
        )
        from src.core.database.database_session import get_db_session

        start = datetime.now(UTC)
        if self.dry_run or self._client is None:
            return AdapterSyncResult(
                sync_kind="reporting",
                started_at=start,
                finished_at=datetime.now(UTC),
                succeeded=False,
                errors={"adapter": "dry-run mode — Improve Digital reporting sync requires live credentials"},
            )

        with get_db_session() as session:
            syncer = ImproveDigitalReportingSync(
                client=self._client,
                tenant_id=self.tenant_id or "default",
                session=session,
                currency=self.currency,
                timezone=self.timezone,
            )
            try:
                inner = syncer.run()
            except ReportingScopeNotGranted as exc:
                return AdapterSyncResult(
                    sync_kind="reporting",
                    started_at=start,
                    finished_at=datetime.now(UTC),
                    succeeded=False,
                    errors={"scope": str(exc)},
                    metadata={"scope_pending": True},
                )
            except ImproveDigitalError as exc:
                return AdapterSyncResult(
                    sync_kind="reporting",
                    started_at=start,
                    finished_at=datetime.now(UTC),
                    succeeded=False,
                    errors={"reporting_client": str(exc)},
                )

        return AdapterSyncResult(
            sync_kind="reporting",
            started_at=start,
            finished_at=datetime.now(UTC),
            succeeded=inner.error is None,
            counts={"line_items": inner.rows_updated, "campaigns": inner.campaigns_covered},
            errors={"job": inner.error} if inner.error else {},
        )

    def latest_reporting_sync_at(self) -> datetime | None:
        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.improvedigital_line_item_stats import (
            ImproveDigitalLineItemStatsRepository,
        )

        with get_db_session() as session:
            return ImproveDigitalLineItemStatsRepository(session, self.tenant_id or "default").latest_sync_at()

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
            campaign_ref = media_buy_id.removeprefix("improvedigital_")
            if action in {"pause_media_buy", "resume_media_buy"}:
                active = str(action == "resume_media_buy").lower()
                self.log(f"Would PUT .../campaigns/{campaign_ref}/line-items/<each>/status?active={active}")
            elif action in {"pause_package", "resume_package"} and package_id:
                active = str(action == "resume_package").lower()
                self.log(f"Would PUT .../campaigns/{campaign_ref}/line-items/{package_id}/status?active={active}")
            elif action in {"update_package_budget", "update_package_impressions"} and package_id and budget:
                field = "budget" if action == "update_package_budget" else "impression_cap"
                self.log(f"Would PUT /rtb/v1/classic/campaigns/{campaign_ref}/line-items/{package_id} {field}={budget}")
            return UpdateMediaBuySuccess(media_buy_id=media_buy_id, affected_packages=[], implementation_date=today)

        assert self._client is not None
        campaign_ref = media_buy_id.removeprefix("improvedigital_")
        if not campaign_ref.isdigit():
            return self._update_error(
                "invalid_media_buy_id",
                f"Cannot derive a Classic campaign ID from media_buy_id {media_buy_id!r}",
            )
        campaign_id = int(campaign_ref)

        try:
            return self._apply_update_action(media_buy_id, campaign_id, action, package_id, budget, today)
        except (ImproveDigitalError, ValueError) as exc:
            logger.warning("Improve Digital update_media_buy %s failed: %s", action, exc)
            return self._update_error("upstream_error", f"Improve Digital rejected the update: {exc}")

    def _apply_update_action(
        self,
        media_buy_id: str,
        campaign_id: int,
        action: str,
        package_id: str | None,
        budget: int | None,
        today: datetime,
    ) -> UpdateMediaBuyResponse:
        """Dispatch one validated update action against the Classic API."""
        from src.core.schemas import AffectedPackage

        assert self._client is not None
        affected: list[AffectedPackage] = []

        if action in {"pause_media_buy", "resume_media_buy", "activate_order"}:
            # Campaign-level state = the state of all its line items.
            active = action != "pause_media_buy"
            listing = self._client.campaigns.list_line_items(campaign_id)
            for line_item in listing.get("line_items") or []:
                self._client.campaigns.set_line_item_status(campaign_id, int(line_item["id"]), active=active)
                affected.append(AffectedPackage(package_id=str(line_item["id"]), paused=not active))
        elif action in {"pause_package", "resume_package"}:
            line_item_id = self._resolve_platform_line_item_id(media_buy_id, package_id)
            if line_item_id is None:
                return self._update_error(
                    "missing_platform_id",
                    f"Package {package_id!r} has no Improve Digital line-item mapping",
                )
            self._client.campaigns.set_line_item_status(
                campaign_id, int(line_item_id), active=(action == "resume_package")
            )
            affected.append(AffectedPackage(package_id=str(package_id), paused=(action == "pause_package")))
        elif action in {"update_package_budget", "update_package_impressions"}:
            if package_id is None or budget is None or budget <= 0:
                return self._update_error(
                    "invalid_update",
                    f"{action} requires a package_id and a positive value (got package_id={package_id!r}, value={budget!r})",
                )
            line_item_id = self._resolve_platform_line_item_id(media_buy_id, package_id)
            if line_item_id is None:
                return self._update_error(
                    "missing_platform_id",
                    f"Package {package_id!r} has no Improve Digital line-item mapping",
                )
            # PUT semantics upstream expect the full DTO — read-modify-write.
            line_item = self._client.campaigns.get_line_item(campaign_id, int(line_item_id))
            field = "budget" if action == "update_package_budget" else "impression_cap"
            line_item[field] = budget
            self._client.campaigns.update_line_item(campaign_id, int(line_item_id), line_item)
            affected.append(AffectedPackage(package_id=str(package_id), paused=False))
        elif action == "archive_order":
            self._client.campaigns.archive_campaign(campaign_id)
        elif action in {"submit_for_approval", "approve_order"}:
            # Classic campaigns have no approval workflow — they are live once
            # active. Treat as a no-op success so standard AdCP lifecycles
            # (submit → approve → activate) pass through cleanly.
            self.log(f"Improve Digital Classic campaigns need no approval — {action} is a no-op")

        return UpdateMediaBuySuccess(media_buy_id=media_buy_id, affected_packages=affected, implementation_date=today)

    def _resolve_platform_line_item_id(self, media_buy_id: str, package_id: str | None) -> str | None:
        """Look up the Classic line-item ID stored for a package by the core
        layer at create time (``package_config["platform_line_item_id"]``)."""
        if not package_id:
            return None
        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.media_buy import MediaBuyRepository

        with get_db_session() as session:
            repo = MediaBuyRepository(session, self.tenant_id or "default")
            package = repo.get_package(media_buy_id, package_id)
            if package is None:
                return None
            platform_id = (package.package_config or {}).get("platform_line_item_id")
            return str(platform_id) if platform_id is not None else None

    @staticmethod
    def _update_error(code: str, message: str) -> UpdateMediaBuyError:
        return UpdateMediaBuyError(errors=[Error(code=code, message=message, details=None)])
