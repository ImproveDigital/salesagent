"""Integration tests for create_media_buy creation path variants.

These tests cover the different ways a buyer can create a media buy and
verify the resulting DB state. Each test exercises a distinct creation
parameter combination or DB-verifiable outcome not covered by existing tests.

Covered gaps (not in any existing integration test):
  PATH-001 — multi-package buy: N packages in request → N MediaPackage rows
  PATH-002 — po_number field preserved in raw_request
  PATH-003 — targeting_overlay on a package preserved in MediaPackage.package_config
  PATH-004 — future-dated buy without creatives → DB status = "pending_creatives"
  PATH-005 — future-dated buy with creatives → DB status = "pending_start"
  PATH-006 — cross-tenant isolation: tenant A's buy is invisible to tenant B's repository
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.core.database.database_session import get_db_session
from src.core.schemas import CreateMediaBuySuccess
from tests.factories.spec_required_kwargs import required_request_kwargs
from tests.integration.media_buy_helpers import (
    _get_tenant_dict,
    _make_create_request,
    make_lifecycle_identity,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.asyncio]


def _future(days: int = 7) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


# ===========================================================================
# PATH-001 — Multi-package buy
# ===========================================================================


class TestMultiPackageBuy:
    """PATH-001: a request with N packages creates exactly N MediaPackage rows.

    WHY THIS TEST EXISTS:
    All other integration tests use a single package. The multi-package path
    exercises a different branch in create_from_packages_bulk and the adapter's
    package iteration. Without a multi-package test, a bug that silently drops
    the second package (e.g. deduplication error or bulk-insert off-by-one)
    would go undetected.
    """

    async def test_two_packages_creates_two_media_package_rows(
        self, sample_tenant, sample_principal, sample_products
    ):
        """PATH-001: 2 packages in request → 2 MediaPackage rows in DB, both linked to same media_buy_id."""
        from src.core.database.models import MediaPackage as DBMediaPackage
        from src.core.schemas import CreateMediaBuyRequest
        from src.core.tools.media_buy_create import _create_media_buy_impl

        tenant_dict = _get_tenant_dict(sample_tenant["tenant_id"])
        identity = make_lifecycle_identity(tenant_dict, sample_principal["principal_id"])

        req = CreateMediaBuyRequest(
            **required_request_kwargs(),
            brand={"domain": "testbrand.com"},
            start_time=_future(1),
            end_time=_future(8),
            packages=[
                {
                    "product_id": "guaranteed_display",
                    "budget": 3000.0,
                    "pricing_option_id": "cpm_usd_fixed",
                },
                {
                    "product_id": "non_guaranteed_video",
                    "budget": 2000.0,
                    "pricing_option_id": "cpm_usd_fixed",
                },
            ],
        )

        result = await _create_media_buy_impl(req=req, identity=identity)

        assert isinstance(result.response, CreateMediaBuySuccess), (
            f"Expected success for multi-package buy. "
            f"Errors: {getattr(result.response, 'errors', None)}"
        )
        media_buy_id = result.response.media_buy_id

        with get_db_session() as session:
            packages = session.scalars(
                select(DBMediaPackage).where(DBMediaPackage.media_buy_id == media_buy_id)
            ).all()

        assert len(packages) == 2, (
            f"Expected 2 MediaPackage rows for a 2-package buy, got {len(packages)}. "
            f"A bug dropping the second package would cause under-delivery on one product."
        )
        package_ids = {p.package_id for p in packages}
        assert len(package_ids) == 2, "Each package must have a distinct package_id."
        for pkg in packages:
            assert pkg.media_buy_id == media_buy_id, (
                "Both MediaPackage rows must reference the same media_buy_id."
            )


# ===========================================================================
# PATH-002 — po_number preserved
# ===========================================================================


class TestPoNumberPreserved:
    """PATH-002: po_number included in request is stored in MediaBuy.raw_request.

    WHY THIS TEST EXISTS:
    po_number is a buyer-supplied purchase order reference used for billing
    reconciliation. It must survive the round-trip into raw_request because
    execute_approved_media_buy reconstructs the CreateMediaBuyRequest from
    raw_request — if po_number is missing, the adapter's order creation may
    not carry the reference.
    """

    async def test_po_number_stored_in_raw_request(
        self, sample_tenant, sample_principal, sample_products
    ):
        """PATH-002: po_number='PO-2026-TEST' in request → found in MediaBuy.raw_request."""
        from src.core.database.models import MediaBuy as DBMediaBuy
        from src.core.schemas import CreateMediaBuyRequest
        from src.core.tools.media_buy_create import _create_media_buy_impl

        po = f"PO-{uuid.uuid4().hex[:8].upper()}"
        tenant_dict = _get_tenant_dict(sample_tenant["tenant_id"])
        identity = make_lifecycle_identity(tenant_dict, sample_principal["principal_id"])

        req = CreateMediaBuyRequest(
            **required_request_kwargs(),
            brand={"domain": "testbrand.com"},
            start_time=_future(1),
            end_time=_future(8),
            po_number=po,
            packages=[
                {"product_id": "guaranteed_display", "budget": 5000.0, "pricing_option_id": "cpm_usd_fixed"}
            ],
        )

        result = await _create_media_buy_impl(req=req, identity=identity)

        assert isinstance(result.response, CreateMediaBuySuccess), (
            f"Errors: {getattr(result.response, 'errors', None)}"
        )

        with get_db_session() as session:
            row = session.scalars(
                select(DBMediaBuy).where(DBMediaBuy.media_buy_id == result.response.media_buy_id)
            ).first()

        raw = row.raw_request or {}
        assert raw.get("po_number") == po, (
            f"po_number must be stored in raw_request for billing reconciliation and "
            f"approval reconstruction. Expected {po!r}, got {raw.get('po_number')!r}."
        )


# ===========================================================================
# PATH-003 — targeting_overlay preserved
# ===========================================================================


class TestTargetingOverlayPreserved:
    """PATH-003: targeting_overlay on a package is preserved in MediaPackage.package_config.

    WHY THIS TEST EXISTS:
    targeting_overlay carries buyer-specified geo/device constraints for a package.
    It must survive into package_config so execute_approved_media_buy can reconstruct
    it for the adapter. Without persistence, re-building a package after manual
    approval would use unconstrained targeting, potentially violating the buyer's
    campaign requirements.
    """

    async def test_geo_targeting_overlay_preserved_in_package_config(
        self, sample_tenant, sample_principal, sample_products
    ):
        """PATH-003: geo_countries targeting_overlay → stored in MediaPackage.package_config."""
        from src.core.database.models import MediaPackage as DBMediaPackage
        from src.core.schemas import CreateMediaBuyRequest
        from src.core.tools.media_buy_create import _create_media_buy_impl

        tenant_dict = _get_tenant_dict(sample_tenant["tenant_id"])
        identity = make_lifecycle_identity(tenant_dict, sample_principal["principal_id"])

        req = CreateMediaBuyRequest(
            **required_request_kwargs(),
            brand={"domain": "testbrand.com"},
            start_time=_future(1),
            end_time=_future(8),
            packages=[
                {
                    "product_id": "guaranteed_display",
                    "budget": 5000.0,
                    "pricing_option_id": "cpm_usd_fixed",
                    "targeting_overlay": {
                        "geo_countries": ["NL", "DE"],
                        "device_type": ["mobile", "desktop"],
                    },
                }
            ],
        )

        result = await _create_media_buy_impl(req=req, identity=identity)

        assert isinstance(result.response, CreateMediaBuySuccess), (
            f"Errors: {getattr(result.response, 'errors', None)}"
        )

        with get_db_session() as session:
            packages = session.scalars(
                select(DBMediaPackage).where(
                    DBMediaPackage.media_buy_id == result.response.media_buy_id
                )
            ).all()

        assert packages, "At least one MediaPackage row must exist."
        pkg_config = packages[0].package_config or {}

        # targeting_overlay may be stored under "targeting_overlay" or "targeting" key
        # (the code stores it under the key it received; both are valid)
        stored_targeting = pkg_config.get("targeting_overlay") or pkg_config.get("targeting")
        assert stored_targeting is not None, (
            "targeting_overlay must be stored in package_config — it is required for "
            "accurate ad targeting reconstruction after manual approval."
        )


# ===========================================================================
# PATH-004 — No creatives → pending_creatives in DB
# ===========================================================================


class TestNoCReativesStatusInDb:
    """PATH-004: buy created without creatives → MediaBuy.status = 'pending_creatives' in DB.

    WHY THIS TEST EXISTS:
    The status determination function (_determine_media_buy_status) is tested in
    isolation by TC-STAT-001 (unit test). This integration test proves that the
    returned status is also the status WRITTEN to the database — unit tests cannot
    verify the DB write, only the function's return value. A mismatch between the
    response status and the DB status would cause get_media_buys to report a
    different status than what the buyer received in the create response.
    """

    async def test_buy_without_creatives_has_pending_creatives_in_db(
        self, sample_tenant, sample_principal, sample_products
    ):
        """PATH-004: no creative_ids in request → MediaBuy.status = 'pending_creatives'."""
        from src.core.database.models import MediaBuy as DBMediaBuy
        from src.core.tools.media_buy_create import _create_media_buy_impl

        tenant_dict = _get_tenant_dict(sample_tenant["tenant_id"])
        identity = make_lifecycle_identity(tenant_dict, sample_principal["principal_id"])

        # No creative_ids on the package — buyer will assign creatives later
        req = _make_create_request()

        result = await _create_media_buy_impl(req=req, identity=identity)
        assert isinstance(result.response, CreateMediaBuySuccess)

        with get_db_session() as session:
            row = session.scalars(
                select(DBMediaBuy).where(DBMediaBuy.media_buy_id == result.response.media_buy_id)
            ).first()

        assert row.status == "pending_creatives", (
            f"A buy without creatives must be stored as 'pending_creatives' in the DB. "
            f"Got {row.status!r}. A status mismatch causes get_media_buys to report "
            f"a different status than what the buyer received in the create response."
        )


# ===========================================================================
# PATH-005 — Future-dated buy with creatives → pending_start in DB
# ===========================================================================


class TestFutureDatedBuyWithCreativesStatus:
    """PATH-005: future-dated buy WITH creatives assigned → DB status = 'pending_start'.

    WHY THIS TEST EXISTS:
    PATH-004 verifies the no-creatives path. This test verifies the orthogonal
    condition: creatives ARE assigned but the start date is in the future. The
    status must be 'pending_start' (waiting for the wall clock, not for creatives).
    Together PATH-004 and PATH-005 pin the two-axis status matrix that buyers
    use to understand what action is needed next.

    To assign creatives at create time, the test uses a creative that already
    exists in the DB (via CreativeFactory-equivalent inline setup) and references
    it via creative_ids on the package.
    """

    async def test_future_dated_buy_with_creative_ids_has_pending_start_in_db(
        self, sample_tenant, sample_principal, sample_products
    ):
        """PATH-005: creative_ids assigned + future start → MediaBuy.status = 'pending_start'."""
        from src.core.database.models import Creative as DBCreative
        from src.core.database.models import MediaBuy as DBMediaBuy
        from src.core.schemas import CreateMediaBuyRequest
        from src.core.tools.media_buy_create import _create_media_buy_impl

        tenant_id = sample_tenant["tenant_id"]
        principal_id = sample_principal["principal_id"]

        # Create a pre-approved creative so the creative_ids assignment is valid
        creative_id = f"cre_path05_{uuid.uuid4().hex[:8]}"
        with get_db_session() as session:
            session.add(DBCreative(
                tenant_id=tenant_id,
                creative_id=creative_id,
                principal_id=principal_id,
                name="Pre-approved Creative",
                agent_url="https://creative.adcontextprotocol.org",
                format="display_300x250",
                status="approved",
                data={
                    "assets": {
                        "banner_image": {
                            "asset_type": "image",
                            "url": "https://example.com/banner.png",
                            "width": 300,
                            "height": 250,
                        }
                    }
                },
            ))
            session.commit()

        tenant_dict = _get_tenant_dict(tenant_id)
        identity = make_lifecycle_identity(tenant_dict, principal_id)

        req = CreateMediaBuyRequest(
            **required_request_kwargs(),
            brand={"domain": "testbrand.com"},
            start_time=_future(days=3),   # clearly in the future
            end_time=_future(days=10),
            packages=[
                {
                    "product_id": "guaranteed_display",
                    "budget": 5000.0,
                    "pricing_option_id": "cpm_usd_fixed",
                    "creative_ids": [creative_id],  # creative assigned at create time
                }
            ],
        )

        result = await _create_media_buy_impl(req=req, identity=identity)
        assert isinstance(result.response, CreateMediaBuySuccess), (
            f"Errors: {getattr(result.response, 'errors', None)}"
        )

        with get_db_session() as session:
            row = session.scalars(
                select(DBMediaBuy).where(DBMediaBuy.media_buy_id == result.response.media_buy_id)
            ).first()

        assert row.status == "pending_start", (
            f"A future-dated buy with creatives must be 'pending_start', not {row.status!r}. "
            f"'pending_start' tells the buyer: creatives are ready, waiting for the clock."
        )


# ===========================================================================
# PATH-006 — Cross-tenant isolation
# ===========================================================================


class TestCrossTenantIsolation:
    """PATH-006: a media buy created under tenant A is invisible to tenant B's repository.

    WHY THIS TEST EXISTS:
    MediaBuyRepository is tenant-scoped (filters by tenant_id on every query). Without
    an explicit integration test, a regression that removes the tenant_id filter would
    allow tenants to read each other's buys. This is the most critical data isolation
    invariant in the system — a unit test with mocked DB cannot prove real SQL scoping.
    """

    async def test_tenant_a_buy_not_visible_to_tenant_b(
        self, sample_tenant, sample_principal, sample_products
    ):
        """PATH-006: MediaBuy created under tenant A is not returned by tenant B's repository."""
        from src.core.database.models import Principal as DBPrincipal
        from src.core.database.models import Tenant as DBTenant
        from src.core.database.repositories import MediaBuyRepository
        from src.core.tools.media_buy_create import _create_media_buy_impl

        # Create the buy under sample_tenant (tenant A)
        tenant_a_dict = _get_tenant_dict(sample_tenant["tenant_id"])
        identity_a = make_lifecycle_identity(tenant_a_dict, sample_principal["principal_id"])
        req = _make_create_request()

        result = await _create_media_buy_impl(req=req, identity=identity_a)
        assert isinstance(result.response, CreateMediaBuySuccess)
        media_buy_id = result.response.media_buy_id

        # Set up tenant B (completely separate tenant)
        tenant_b_id = f"tenant_b_{uuid.uuid4().hex[:8]}"
        now = datetime.now(UTC)
        with get_db_session() as session:
            session.add(DBTenant(
                tenant_id=tenant_b_id, name="Tenant B",
                subdomain=f"tenant-b-{uuid.uuid4().hex[:6]}",
                is_active=True, ad_server="mock",
                human_review_required=False,
                created_at=now, updated_at=now,
            ))
            session.commit()

        # Tenant B's repository must NOT see tenant A's buy
        with get_db_session() as session:
            repo_b = MediaBuyRepository(session, tenant_b_id)
            result_from_b = repo_b.get_by_id(media_buy_id)

        assert result_from_b is None, (
            f"MediaBuy {media_buy_id!r} created under {sample_tenant['tenant_id']!r} "
            f"must NOT be visible to tenant {tenant_b_id!r}. "
            f"Cross-tenant data leakage would expose buyer campaign data to competitors."
        )
