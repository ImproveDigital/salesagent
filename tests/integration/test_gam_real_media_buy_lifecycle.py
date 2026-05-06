"""Stage 2: real-GAM end-to-end lifecycle against the Wonderstruck test network.

Same _impl flow as ``test_basic_media_buy_lifecycle`` — only the adapter
swaps. Uses factory-boy + IntegrationEnv (per tests/CLAUDE.md), no inline
``session.add`` or ``get_db_session`` in the test body.

Requires env vars in ``.env``:
  - WONDERSTRUCK_SERVICE_KEY_FILE  (path to service account JSON)
  - WONDERSTRUCK_NETWORK_CODE      (network code as string)

Wonderstruck test network constants (probed via scripts/probe_wonderstruck.py):
  - Network: 23312659540 (Wonderstruck Productions LLC, USD, America/New_York)
  - Advertiser: 5934447726 (Scope3_test)
  - Ad units: 23313239368 (Top banner), 23329617233 (Middle Rectangle) — both 300x250
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from src.core.database.models import Creative as DBCreative
from src.core.database.models import MediaBuy as DBMediaBuy
from src.core.database.models import MediaPackage as DBMediaPackage
from src.core.resolved_identity import ResolvedIdentity
from tests.factories import (
    AdapterConfigFactory,
    GAMInventoryFactory,
    PricingOptionFactory,
    PrincipalFactory,
    ProductFactory,
    ProductInventoryMappingFactory,
    PropertyTagFactory,
    TenantFactory,
)
from tests.harness._base import IntegrationEnv
from tests.helpers.gam_test_config import non_guaranteed_cpm_impl_config
from tests.integration.media_buy_helpers import make_lifecycle_identity

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_db,
    pytest.mark.requires_gam,
    pytest.mark.asyncio,
]


WONDERSTRUCK_NETWORK_CODE = "23312659540"
WONDERSTRUCK_ADVERTISER_ID = "5934447726"
WONDERSTRUCK_AD_UNIT_IDS = ["23313239368", "23329617233"]
WONDERSTRUCK_FORMAT_ID = "display_300x250"

GAM_TENANT_ID = "gam_real_lifecycle_tenant"
GAM_PRINCIPAL_ID = "gam_real_lifecycle_principal"
GAM_PRODUCT_ID = "gam_real_lifecycle_product"


def _load_env() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def _service_account_json() -> str | None:
    _load_env()
    key_file = os.environ.get("WONDERSTRUCK_SERVICE_KEY_FILE")
    if key_file and Path(key_file).is_file():
        return Path(key_file).read_text()
    return None


@pytest.fixture
def wonderstruck_creds():
    sa_json = _service_account_json()
    if not sa_json:
        pytest.skip("Wonderstruck creds not available (set WONDERSTRUCK_SERVICE_KEY_FILE)")
    return sa_json


@pytest.fixture
def gam_order_archive_cleanup(wonderstruck_creds):
    """Archive every non-archived order on the test advertiser at teardown.

    Scope3_test is a dedicated test advertiser — sweeping all of its
    non-archived orders survives the case where ``_create_media_buy_impl``
    crashes after creating the GAM order but before the test tracks the id.
    """
    from googleads import ad_manager

    from src.adapters.gam.client import GAMClientManager

    cm = GAMClientManager(
        {"service_account_json": wonderstruck_creds},
        network_code=WONDERSTRUCK_NETWORK_CODE,
    )
    order_service = cm.get_service("OrderService")

    created_orders: list[str] = []

    yield created_orders

    try:
        sb = ad_manager.StatementBuilder()
        sb.Where("advertiserId = :a AND isArchived = :archived").WithBindVariable(
            "a", int(WONDERSTRUCK_ADVERTISER_ID)
        ).WithBindVariable("archived", False).Limit(50)
        page = order_service.getOrdersByStatement(sb.ToStatement())
        results = getattr(page, "results", None) or []
        for order in results:
            order_id = str(getattr(order, "id", ""))
            if not order_id:
                continue
            try:
                sb2 = ad_manager.StatementBuilder()
                sb2.Where("id = :id").WithBindVariable("id", int(order_id))
                order_service.performOrderAction({"xsi_type": "ArchiveOrders"}, sb2.ToStatement())
                print(f"INFO: archived GAM order {order_id}")
            except Exception as exc:  # pragma: no cover - cleanup best effort
                print(f"WARN: failed to archive GAM order {order_id}: {exc}")
    except Exception as exc:  # pragma: no cover
        print(f"WARN: cleanup sweep failed: {exc}")


def _seed_gam_tenant(wonderstruck_creds: str):
    """Build the full test fixture set via factories. Caller must hold an
    IntegrationEnv that's bound the factory sessions.
    """
    tenant = TenantFactory(
        tenant_id=GAM_TENANT_ID,
        name="GAM Real Lifecycle Tenant",
        subdomain="gamreal",
        ad_server="google_ad_manager",
        human_review_required=False,
        # Skip auto-naming: no tenant-level Gemini key.
        auto_naming_enabled=False,
    )
    PropertyTagFactory(
        tenant=tenant,
        tag_id="all_inventory",
        name="All",
        description="All inventory",
    )

    AdapterConfigFactory(
        tenant=tenant,
        adapter_type="google_ad_manager",
        gam_network_code=WONDERSTRUCK_NETWORK_CODE,
        gam_auth_method="service_account",
        gam_manual_approval_required=False,
        # Factory routes plaintext through the encrypted-at-rest setter.
        gam_service_account_json_plaintext=wonderstruck_creds,
    )

    PrincipalFactory(
        tenant=tenant,
        principal_id=GAM_PRINCIPAL_ID,
        name="GAM Real Test Principal",
        platform_mappings={
            "google_ad_manager": {"advertiser_id": WONDERSTRUCK_ADVERTISER_ID},
        },
    )

    for ad_unit_id in WONDERSTRUCK_AD_UNIT_IDS:
        GAMInventoryFactory(
            tenant=tenant,
            inventory_type="AD_UNIT",
            inventory_id=ad_unit_id,
            name=f"Ad Unit {ad_unit_id}",
            status="ACTIVE",
        )

    product = ProductFactory(
        tenant=tenant,
        product_id=GAM_PRODUCT_ID,
        name="Real GAM Display 300x250",
        description="Wonderstruck display",
        format_ids=[{"agent_url": "https://creative.adcontextprotocol.org", "id": WONDERSTRUCK_FORMAT_ID}],
        targeting_template={},
        delivery_type="non_guaranteed",
        property_tags=["all_inventory"],
        implementation_config=non_guaranteed_cpm_impl_config(
            targeted_ad_unit_ids=list(WONDERSTRUCK_AD_UNIT_IDS),
            order_name_template="AdCP-E2E-{po_number}-{date_range}",
        ),
    )

    for ad_unit_id in WONDERSTRUCK_AD_UNIT_IDS:
        ProductInventoryMappingFactory(
            tenant_id=GAM_TENANT_ID,
            product_id=GAM_PRODUCT_ID,
            inventory_type="AD_UNIT",
            inventory_id=ad_unit_id,
        )

    PricingOptionFactory(
        product=product,
        pricing_model="cpm",
        rate=1.00,
        currency="USD",
        is_fixed=True,
    )

    return tenant


def _identity() -> ResolvedIdentity:
    return make_lifecycle_identity(
        {
            "tenant_id": GAM_TENANT_ID,
            "name": "GAM Real Lifecycle Tenant",
            "subdomain": "gamreal",
            "ad_server": "google_ad_manager",
            "human_review_required": False,
            "brand_manifest_policy": "public",
        },
        GAM_PRINCIPAL_ID,
        test_session_id="gam-real-lifecycle",
    )


def _future(days: int) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


class TestGAMRealMediaBuyLifecycle:
    """4-phase lifecycle, in-process, real Wonderstruck network."""

    async def test_full_lifecycle_against_real_gam(
        self,
        integration_db,
        wonderstruck_creds,
        gam_order_archive_cleanup,
    ):
        from src.core.schemas import (
            CreateMediaBuyRequest,
            GetMediaBuyDeliveryRequest,
            GetProductsRequest,
        )
        from src.core.tools.creatives._sync import _sync_creatives_impl
        from src.core.tools.media_buy_create import _create_media_buy_impl
        from src.core.tools.media_buy_delivery import _get_media_buy_delivery_impl
        from src.core.tools.products import _get_products_impl

        with IntegrationEnv(tenant_id=GAM_TENANT_ID, principal_id=GAM_PRINCIPAL_ID):
            _seed_gam_tenant(wonderstruck_creds)
            identity = _identity()

            # ───── Phase 1: get_products ─────
            products_resp = await _get_products_impl(
                GetProductsRequest(brand={"domain": "testbrand.com"}, brief="display"),
                identity,
            )
            product_ids = [p.product_id for p in products_resp.products]
            assert GAM_PRODUCT_ID in product_ids, f"Expected {GAM_PRODUCT_ID} in catalog, got {product_ids}"

            # ───── Phase 2: create_media_buy (real GAM order) ─────
            create_req = CreateMediaBuyRequest(
                brand={"domain": "testbrand.com"},
                start_time=_future(1),
                end_time=_future(8),
                po_number=f"E2E-{uuid.uuid4().hex[:6]}",
                packages=[
                    {
                        "product_id": GAM_PRODUCT_ID,
                        "budget": 100.0,
                        "pricing_option_id": "cpm_usd_fixed",
                    }
                ],
            )
            create_result = await _create_media_buy_impl(req=create_req, identity=identity)

            assert create_result.status not in ("failed",), (
                f"create_media_buy failed: status={create_result.status}, "
                f"errors={getattr(create_result.response, 'errors', None)}"
            )
            media_buy_id = create_result.response.media_buy_id
            assert media_buy_id, f"missing media_buy_id: {create_result.response}"
            gam_order_archive_cleanup.append(media_buy_id)

            # Read-only DB inspection — repository would be heavier than the
            # value here. The harness-bound session is available.
            from src.core.database.database_session import get_db_session

            with get_db_session() as session:
                mb = session.scalars(select(DBMediaBuy).where(DBMediaBuy.media_buy_id == media_buy_id)).first()
                assert mb is not None, f"MediaBuy {media_buy_id} not persisted"
                packages = session.scalars(
                    select(DBMediaPackage).where(DBMediaPackage.media_buy_id == media_buy_id)
                ).all()
                assert packages, f"No MediaPackage rows for {media_buy_id}"
                li_ids = [p.package_config.get("platform_line_item_id") for p in packages]
                assert any(li_ids), f"Expected platform_line_item_id on package: {li_ids}"
                first_package_id = packages[0].package_id

            # ───── Phase 3: sync_creatives ─────
            creative_id = f"cr_{uuid.uuid4().hex[:8]}"
            sync_resp = _sync_creatives_impl(
                creatives=[
                    {
                        "creative_id": creative_id,
                        "name": "GAM Real Test Creative",
                        "format": WONDERSTRUCK_FORMAT_ID,
                        "format_id": {
                            "agent_url": "https://creative.adcontextprotocol.org",
                            "id": WONDERSTRUCK_FORMAT_ID,
                        },
                        "media_url": "https://example.com/300x250.png",
                        "click_through_url": "https://example.com/landing",
                    }
                ],
                assignments={creative_id: [first_package_id]},
                identity=identity,
            )
            assert any(c.creative_id == creative_id for c in (sync_resp.creatives or [])), (
                f"Synced creative {creative_id} missing from response: {sync_resp}"
            )

            with get_db_session() as session:
                cr = session.scalars(
                    select(DBCreative).where(
                        DBCreative.creative_id == creative_id,
                        DBCreative.tenant_id == GAM_TENANT_ID,
                    )
                ).first()
                assert cr is not None, f"Creative {creative_id} not persisted"

            # ───── Phase 4: get_media_buy_delivery (real GAM ReportingService) ─────
            delivery_resp = _get_media_buy_delivery_impl(
                GetMediaBuyDeliveryRequest(
                    media_buy_ids=[media_buy_id],
                    start_date=datetime.now(UTC).date().isoformat(),
                    end_date=(datetime.now(UTC) + timedelta(days=8)).date().isoformat(),
                ),
                identity,
            )
            assert not delivery_resp.errors, f"delivery errors: {delivery_resp.errors}"
            deliveries = delivery_resp.media_buy_deliveries or []
            assert media_buy_id in [d.media_buy_id for d in deliveries], (
                f"Expected {media_buy_id} in delivery response, got {[d.media_buy_id for d in deliveries]}"
            )
