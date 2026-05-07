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
    TenantAuthConfigFactory,
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
def wonderstruck_sa_can_approve_orders(wonderstruck_creds):
    """Skip unless the Wonderstruck SA has a role that can approve orders.

    GAM's ``OrderActionService.performOrderAction(approve)`` requires the
    Approve Orders permission. The Trafficker built-in role doesn't include
    it; Sales Manager / Salesperson / Admin do. See issue #45 — until the
    Wonderstruck SA is upgraded, any test that asserts post-DRAFT
    progression cannot pass.

    Returns the SA's role name on success.
    """
    from src.adapters.gam.client import GAMClientManager

    cm = GAMClientManager(
        {"service_account_json": wonderstruck_creds},
        network_code=WONDERSTRUCK_NETWORK_CODE,
    )
    try:
        user = cm.get_service("UserService").getCurrentUser()
    except Exception as exc:
        # Transient network / proxy errors during role probe shouldn't
        # explode the test — skip with the underlying reason so re-runs
        # naturally pick up the role once GAM is reachable again.
        pytest.skip(f"GAM UserService probe failed: {exc!r}. Re-run when GAM is reachable.")

    # zeep ComplexType doesn't support .get(); fall back to attr access.
    try:
        role_name = user["roleName"]
    except (KeyError, AttributeError):
        role_name = getattr(user, "roleName", "") or ""

    role_name = str(role_name)
    if role_name == "Trafficker":
        pytest.skip(
            f"Wonderstruck SA role is {role_name!r}. Order approval requires "
            f"Sales Manager (or any role with the 'Approve Orders' permission). "
            f"See issue #45 — upgrade the SA's GAM role to enable this test."
        )
    return role_name


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
        # validate_setup_complete also requires SSO config + auth_setup_mode off.
        # See #43.
        auth_setup_mode=False,
    )
    TenantAuthConfigFactory(tenant=tenant, oidc_enabled=True)
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
            # Delivery assertion tolerates "adapter_error" on brand-new orders.
            # The Phase-4 *contract* under test here is "the impl reaches the
            # GAM ReportingService and returns a response shape", not "the
            # report has metrics yet". The adapter wraps the underlying
            # "GAM data is not fresh enough" ValueError as a generic
            # adapter_error code at media_buy_delivery.py:347, which fires
            # reliably for orders stuck in DRAFT because the Wonderstruck SA
            # still lacks sales-manager permissions (#45). Tightens to a full
            # delivery assertion when #45 lands.
            errors = delivery_resp.errors or []
            unexpected = [e for e in errors if getattr(e, "code", None) != "adapter_error"]
            assert not unexpected, f"Unexpected delivery errors: {unexpected}"


class TestGAMOrderProgressesPastDraft:
    """Issue #45: Verify orders progress past DRAFT once the Wonderstruck SA
    has Sales Manager (or any role with the 'Approve Orders' permission).

    Skips automatically while the SA is still Trafficker. The moment the
    GAM Admin role is upgraded, this test starts running and proves:
      1. ``performOrderAction(approve)`` succeeds (no PERMISSION_DENIED).
      2. Order status leaves DRAFT (transitions to APPROVED / READY / DELIVERING).
      3. Background approval polling task converges instead of looping.

    Delivery freshness is intentionally NOT asserted here. Even with the SA
    upgraded, GAM ReportingService data takes time to sync for a brand-new
    order; the freshness check at ``google_ad_manager.py:1135`` will still
    surface as ``adapter_error`` for orders created seconds ago. That timing
    is a separate concern from "can the SA approve orders" and should be
    validated by a delivery-after-impressions test (a candidate for #46 once
    we figure out how to seed delivery in the sandbox).
    """

    async def test_order_status_leaves_draft_after_approval(
        self,
        integration_db,
        wonderstruck_creds,
        wonderstruck_sa_can_approve_orders,
        gam_order_archive_cleanup,
    ):
        from googleads import ad_manager

        from src.adapters.gam.client import GAMClientManager
        from src.core.schemas import (
            CreateMediaBuyRequest,
            GetMediaBuyDeliveryRequest,
        )
        from src.core.tools.media_buy_create import _create_media_buy_impl
        from src.core.tools.media_buy_delivery import _get_media_buy_delivery_impl

        with IntegrationEnv(tenant_id=GAM_TENANT_ID, principal_id=GAM_PRINCIPAL_ID):
            _seed_gam_tenant(wonderstruck_creds)
            identity = _identity()

            create_result = await _create_media_buy_impl(
                req=CreateMediaBuyRequest(
                    brand={"domain": "testbrand.com"},
                    start_time=_future(1),
                    end_time=_future(8),
                    po_number=f"E2E-#45-{uuid.uuid4().hex[:6]}",
                    packages=[
                        {
                            "product_id": GAM_PRODUCT_ID,
                            "budget": 100.0,
                            "pricing_option_id": "cpm_usd_fixed",
                        }
                    ],
                ),
                identity=identity,
            )
            assert create_result.status not in ("failed",), (
                f"create_media_buy failed: errors={getattr(create_result.response, 'errors', None)}"
            )
            order_id = create_result.response.media_buy_id
            assert order_id
            gam_order_archive_cleanup.append(order_id)

            # Poll OrderService for the order's current status. The adapter
            # kicks off a background approval polling task on order creation;
            # once forecasting catches up GAM auto-approves. Bound the wait
            # at ~60s — Wonderstruck sandbox is small and forecast is fast.
            cm = GAMClientManager(
                {"service_account_json": wonderstruck_creds},
                network_code=WONDERSTRUCK_NETWORK_CODE,
            )
            order_service = cm.get_service("OrderService")
            terminal_statuses = {"APPROVED", "READY", "DELIVERING", "PAUSED", "COMPLETED"}

            from time import sleep, time

            deadline = time() + 60
            final_status = "DRAFT"
            while time() < deadline:
                sb = ad_manager.StatementBuilder()
                sb.Where("id = :id").WithBindVariable("id", int(order_id))
                page = order_service.getOrdersByStatement(sb.ToStatement())
                results = getattr(page, "results", None) or []
                if not results:
                    break
                final_status = str(getattr(results[0], "status", "") or "")
                if final_status in terminal_statuses:
                    break
                sleep(3)

            assert final_status in terminal_statuses, (
                f"Order {order_id} stayed in status={final_status!r} after 60s. "
                f"Expected one of {sorted(terminal_statuses)}. "
                f"If the Wonderstruck SA was just upgraded, the background "
                f"approval polling task may need longer than 60s on first run."
            )

            # Sanity: the impl reaches the ReportingService (the call is in
            # the audit log even when freshness blocks the metrics). We
            # don't assert no errors here because GAM ReportingService data
            # for a brand-new order isn't immediately fresh — adapter_error
            # is acceptable. Strict-delivery validation belongs in a
            # separate test that controls when impressions land in the
            # sandbox; out of #45 scope.
            delivery_resp = _get_media_buy_delivery_impl(
                GetMediaBuyDeliveryRequest(
                    media_buy_ids=[order_id],
                    start_date=datetime.now(UTC).date().isoformat(),
                    end_date=(datetime.now(UTC) + timedelta(days=8)).date().isoformat(),
                ),
                identity,
            )
            errors = delivery_resp.errors or []
            unexpected = [e for e in errors if getattr(e, "code", None) != "adapter_error"]
            assert not unexpected, f"Unexpected delivery errors: {unexpected}"
