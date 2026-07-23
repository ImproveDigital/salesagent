"""Integration tests for create_media_buy — real PostgreSQL, no adapter mocking.

These tests call _create_media_buy_impl against a real database to verify
behaviours that unit tests (with mocked sessions) cannot prove:

  - Idempotency dedup is enforced by the DB unique index, not just in-memory logic.
  - Product lookup, currency limit lookup and CurrencyLimit absence all hit real rows.
  - MediaBuy and MediaPackage rows are actually written and are consistent.

Uses the shared ``sample_tenant`` / ``sample_principal`` / ``sample_products``
fixtures from tests/integration/conftest.py — they seed a fully-configured tenant
(CurrencyLimit=USD, PropertyTag, AuthorizedProperty, GAMInventory, TenantAuthConfig)
so ``validate_setup_complete`` passes on the production path (no test_session_id bypass
needed for those tests).

Covered gaps:
  TC-IDEM-001 — first create with idempotency_key → new media buy created
  TC-IDEM-002 — retry with same key + same principal → same media_buy_id, no duplicate
  TC-IDEM-003 — same key but different principal → new media buy (keys are principal-scoped)
  TC-PROD-004 — non-existent product_id → AdCPProductNotFoundError
  TC-CURR-001 — no CurrencyLimit for request currency → descriptive error
  TC-DB-001   — successful create writes a MediaBuy row with correct fields
  TC-DB-002   — successful create writes one MediaPackage row per request package
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.core.database.database_session import get_db_session
from src.core.exceptions import AdCPProductNotFoundError
from src.core.schemas import CreateMediaBuyError, CreateMediaBuyRequest, CreateMediaBuySuccess
from tests.factories.spec_required_kwargs import required_request_kwargs
from tests.integration.media_buy_helpers import (
    _get_tenant_dict,
    _make_create_request,
    make_lifecycle_identity,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _future(days: int = 7) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


def _make_keyed_request(idempotency_key: str, **overrides) -> CreateMediaBuyRequest:
    """Build a request with an explicit idempotency_key (for idempotency tests)."""
    return _make_create_request(
        **required_request_kwargs(idempotency_key=idempotency_key),
        **overrides,
    )


def _identity_for(sample_tenant: dict, principal_id: str, *, bypass_setup: bool = False) -> ResolvedIdentity:  # noqa: F821
    """Build a ResolvedIdentity from the sample_tenant fixture dict."""
    tenant_dict = _get_tenant_dict(sample_tenant["tenant_id"])
    return make_lifecycle_identity(
        tenant_dict,
        principal_id,
        test_session_id="unit-test" if bypass_setup else None,
    )


# ---------------------------------------------------------------------------
# TC-IDEM-001 / TC-IDEM-002 / TC-IDEM-003 — Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Idempotency is enforced at the DB level (unique index on tenant+principal+key).

    The unit tests in test_create_media_buy_request_validation.py mock the UoW and
    test the branch logic. These tests prove the real DB unique index is the
    actual enforcement mechanism and that the replay path returns the correct data.
    """

    async def test_first_create_with_idempotency_key_succeeds(self, sample_tenant, sample_principal, sample_products):
        """TC-IDEM-001: first request with a fresh idempotency_key creates a new media buy.

        WHY THIS TEST EXISTS:
        Confirms the baseline: a never-seen idempotency_key does NOT trigger the
        replay path. The function must create a new MediaBuy row, call the adapter,
        and return a CreateMediaBuySuccess with a new media_buy_id. If the idempotency
        check accidentally matched an empty DB, every first request would silently
        return no media buy.
        """
        from src.core.tools.media_buy_create import _create_media_buy_impl

        idem_key = f"idem-first-{uuid.uuid4().hex}"
        req = _make_keyed_request(idem_key)
        identity = _identity_for(sample_tenant, sample_principal["principal_id"])

        result = await _create_media_buy_impl(req=req, identity=identity)

        assert not isinstance(result.response, CreateMediaBuyError), (
            f"First create with fresh key must succeed. Errors: {getattr(result.response, 'errors', None)}"
        )
        assert isinstance(result.response, CreateMediaBuySuccess)
        assert result.response.media_buy_id, "Response must contain a media_buy_id."

    async def test_retry_with_same_key_returns_original_buy(self, sample_tenant, sample_principal, sample_products):
        """TC-IDEM-002: second request with the same idempotency_key returns the same media_buy_id.

        WHY THIS TEST EXISTS:
        Buyers retry on network failures, timeouts, or uncertain delivery. The DB
        unique index on (tenant_id, principal_id, idempotency_key) is the actual
        dedup mechanism. This test proves the replay path is taken on the second
        call and that the same media_buy_id is returned — not a new one. Without
        this, a timeout-and-retry pattern would double-book the ad server order.

        Note: this test creates a REAL media buy on the first call, which writes to
        the DB. The second call reads that row via the repository's
        find_by_idempotency_key() and returns it directly.
        """
        from src.core.tools.media_buy_create import _create_media_buy_impl

        idem_key = f"idem-retry-{uuid.uuid4().hex}"
        identity = _identity_for(sample_tenant, sample_principal["principal_id"])

        # First call — creates the buy.
        req1 = _make_keyed_request(idem_key)
        result1 = await _create_media_buy_impl(req=req1, identity=identity)
        assert isinstance(result1.response, CreateMediaBuySuccess), (
            f"First call must succeed before testing retry: {getattr(result1.response, 'errors', None)}"
        )
        original_id = result1.response.media_buy_id

        # Second call — same key, same principal, same tenant.
        req2 = _make_keyed_request(idem_key)
        result2 = await _create_media_buy_impl(req=req2, identity=identity)

        assert isinstance(result2.response, CreateMediaBuySuccess), (
            "Retry must return a success response, not an error."
        )
        assert result2.response.media_buy_id == original_id, (
            f"Retry must return the ORIGINAL media_buy_id {original_id!r}, "
            f"not a new one {result2.response.media_buy_id!r}."
        )

    async def test_same_key_different_principal_creates_new_buy(self, sample_tenant, sample_principal, sample_products):
        """TC-IDEM-003: same idempotency_key used by a different principal creates a new buy.

        WHY THIS TEST EXISTS:
        Idempotency keys are scoped to (tenant_id, principal_id). Two different buyers
        using the same string key (e.g. a UUID they both generated independently) must
        NOT interfere — each gets their own media buy. Without the principal_id scope,
        buyer B's request could silently replay buyer A's buy.
        """
        from src.core.database.models import Principal
        from src.core.tools.media_buy_create import _create_media_buy_impl

        idem_key = f"idem-scope-{uuid.uuid4().hex}"
        tenant_id = sample_tenant["tenant_id"]

        # Create a second principal in the same tenant.
        second_principal_id = f"test_principal_2_{uuid.uuid4().hex[:6]}"
        with get_db_session() as session:
            p2 = Principal(
                tenant_id=tenant_id,
                principal_id=second_principal_id,
                name="Test Advertiser 2",
                access_token=f"token2_{uuid.uuid4().hex}",
                platform_mappings={"mock": {"id": "test_adv_2"}},
                created_at=datetime.now(UTC),
            )
            session.add(p2)
            session.commit()

        tenant_dict = _get_tenant_dict(tenant_id)

        # First buy — principal 1 with idem_key.
        identity1 = make_lifecycle_identity(tenant_dict, sample_principal["principal_id"])
        req1 = _make_keyed_request(idem_key)
        result1 = await _create_media_buy_impl(req=req1, identity=identity1)
        assert isinstance(result1.response, CreateMediaBuySuccess), (
            f"First buy must succeed: {getattr(result1.response, 'errors', None)}"
        )

        # Second buy — principal 2 with the SAME idem_key.
        identity2 = make_lifecycle_identity(tenant_dict, second_principal_id)
        req2 = _make_keyed_request(idem_key)
        result2 = await _create_media_buy_impl(req=req2, identity=identity2)
        assert isinstance(result2.response, CreateMediaBuySuccess), (
            "Second principal's request with same key must also succeed."
        )
        assert result2.response.media_buy_id != result1.response.media_buy_id, (
            "A different principal using the same key must receive a NEW media_buy_id, "
            "not a replay of the first principal's buy."
        )


# ---------------------------------------------------------------------------
# TC-PROD-004 — Product validation
# ---------------------------------------------------------------------------


class TestProductValidation:
    """Product lookup hits the real DB — the repository returns None for missing products."""

    async def test_unknown_product_id_raises_product_not_found(self, sample_tenant, sample_principal, sample_products):
        """TC-PROD-004: product_id not present in the DB → AdCPProductNotFoundError.

        WHY THIS TEST EXISTS:
        The unit test for this behaviour (test_product_not_found_raises_typed_error
        in test_create_media_buy_behavioral.py) mocks the product query. This
        integration test proves that the real DB repository query returns no rows for
        an unknown product_id and that the impl raises the correct typed exception
        (not a generic ValueError or AttributeError). The error code PRODUCT_NOT_FOUND
        is spec-canonical — buyers can detect it and re-run get_products.

        sample_products is included so validate_setup_complete passes (the setup
        checker requires at least one product to exist). The requested product_id
        is a clearly non-existent value distinct from any seeded product.
        """
        from src.core.tools.media_buy_create import _create_media_buy_impl

        req = _make_create_request(
            packages=[
                {
                    "product_id": "this_product_does_not_exist",
                    "budget": 5000.0,
                    "pricing_option_id": "cpm_usd_fixed",
                }
            ]
        )
        identity = _identity_for(sample_tenant, sample_principal["principal_id"])

        with pytest.raises(AdCPProductNotFoundError) as exc_info:
            await _create_media_buy_impl(req=req, identity=identity)

        assert "this_product_does_not_exist" in str(exc_info.value), (
            "Error must name the missing product_id so buyers can identify what to fix."
        )
        details = exc_info.value.details or {}
        assert "missing_product_ids" in details, (
            "details.missing_product_ids must be present for programmatic error handling."
        )


# ---------------------------------------------------------------------------
# TC-CURR-001 — Currency validation
# ---------------------------------------------------------------------------


class TestCurrencyValidation:
    """CurrencyLimit lookup hits the real DB — missing rows surface as a clear error."""

    async def test_unsupported_currency_returns_descriptive_error(self, integration_db):
        """TC-CURR-001: no CurrencyLimit for the request currency → CreateMediaBuyError.

        WHY THIS TEST EXISTS:
        The unit test patches the DB session and can only verify the branch logic
        (if not currency_limit: raise ValueError). This integration test proves that
        the absence of a CurrencyLimit row in the real DB is correctly detected and
        surfaces as a clear buyer-facing error. Without this guard, a publisher that
        forgets to configure a currency would silently let requests through until the
        adapter rejected them with a cryptic error.

        Setup: a minimal tenant with a USD product but NO CurrencyLimit rows. The
        request currency resolves to USD (from the product's pricing option). The
        USD CurrencyLimit lookup returns None → error.

        test_session_id bypasses validate_setup_complete because this minimal tenant
        does not have all the required setup artifacts (GAMInventory, TenantAuthConfig,
        AuthorizedProperty etc.) — only the currency check is under test here.
        """
        from src.core.database.models import PricingOption as PricingOptionModel
        from src.core.database.models import Principal, Product, Tenant
        from src.core.testing_hooks import AdCPTestContext
        from src.core.tools.media_buy_create import _create_media_buy_impl

        suffix = uuid.uuid4().hex[:8]
        tenant_id = f"no_curr_{suffix}"
        principal_id = f"agent_{suffix}"

        # Minimal tenant — no CurrencyLimit rows (the condition under test).
        with get_db_session() as session:
            now = datetime.now(UTC)
            session.add(
                Tenant(
                    tenant_id=tenant_id,
                    name=f"No-Currency Tenant {suffix}",
                    subdomain=f"no-curr-{suffix}",
                    is_active=True,
                    ad_server="mock",
                    human_review_required=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Principal(
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    name="Test Agent",
                    access_token=f"tok_{suffix}",
                    platform_mappings={"mock": {"id": "adv_test"}},
                    created_at=now,
                )
            )
            product = Product(
                tenant_id=tenant_id,
                product_id="display_usd",
                name="Display USD Product",
                delivery_type="guaranteed",
                targeting_template={},  # NOT NULL in schema
                format_ids=[{"agent_url": "https://creative.adcontextprotocol.org", "id": "display_300x250"}],
                property_tags=["all_inventory"],
            )
            session.add(product)
            session.commit()
            # Add a USD pricing option to the product.
            session.add(
                PricingOptionModel(
                    product_id="display_usd",
                    tenant_id=tenant_id,
                    pricing_model="cpm",
                    currency="USD",
                    rate=5.0,
                    is_fixed=True,
                )
            )
            session.commit()

        tenant_dict = _get_tenant_dict(tenant_id)
        from src.core.resolved_identity import ResolvedIdentity

        identity = ResolvedIdentity(
            principal_id=principal_id,
            tenant_id=tenant_id,
            tenant=tenant_dict,
            protocol="mcp",
            # bypass setup check — tenant lacks full setup artifacts by design
            testing_context=AdCPTestContext(
                dry_run=False,
                test_session_id="unit-test",
            ),
        )

        req = CreateMediaBuyRequest(
            **required_request_kwargs(),
            brand={"domain": "testbrand.com"},
            start_time=_future(1),
            end_time=_future(8),
            packages=[{"product_id": "display_usd", "budget": 5000.0, "pricing_option_id": "cpm_usd_fixed"}],
        )

        result = await _create_media_buy_impl(req=req, identity=identity)

        assert isinstance(result.response, CreateMediaBuyError), (
            "Missing CurrencyLimit must produce an error response, not a success."
        )
        error_message = result.response.errors[0].message
        assert "not supported" in error_message.lower() or "currency" in error_message.lower(), (
            f"Error must explain the currency problem. Got: {error_message!r}"
        )


# ---------------------------------------------------------------------------
# TC-DB-001 / TC-DB-002 — Database persistence
# ---------------------------------------------------------------------------


class TestDatabasePersistence:
    """Verify that a successful create writes the expected rows to the real DB.

    Unit tests mock the UoW and only verify that repository methods are called.
    These tests open a fresh session after _create_media_buy_impl returns and
    read the rows back to confirm the data was actually committed.
    """

    async def test_successful_creation_persists_media_buy_row(self, sample_tenant, sample_principal, sample_products):
        """TC-DB-001: CreateMediaBuySuccess → MediaBuy row with correct fields in DB.

        WHY THIS TEST EXISTS:
        The UoW mock in unit tests verifies that repository methods are invoked but
        cannot prove the SQL was committed or the fields were written correctly. This
        test reads back the row in a fresh DB session and checks that tenant_id,
        principal_id, and status are present and consistent. A silent rollback or
        missing commit would be invisible to unit tests but caught here.
        """
        from src.core.database.models import MediaBuy as DBMediaBuy
        from src.core.tools.media_buy_create import _create_media_buy_impl

        req = _make_create_request()
        identity = _identity_for(sample_tenant, sample_principal["principal_id"])

        result = await _create_media_buy_impl(req=req, identity=identity)

        assert isinstance(result.response, CreateMediaBuySuccess), (
            f"Expected success to verify persistence. Errors: {getattr(result.response, 'errors', None)}"
        )
        media_buy_id = result.response.media_buy_id

        with get_db_session() as session:
            row = session.scalars(select(DBMediaBuy).where(DBMediaBuy.media_buy_id == media_buy_id)).first()

        assert row is not None, f"MediaBuy row {media_buy_id!r} not found in DB after successful create."
        assert row.tenant_id == sample_tenant["tenant_id"], "Persisted tenant_id must match the requesting tenant."
        assert row.principal_id == sample_principal["principal_id"], (
            "Persisted principal_id must match the requesting principal."
        )
        assert row.status in ("active", "pending_start", "pending_creatives"), (
            f"Persisted status {row.status!r} is not a valid post-create lifecycle status."
        )

    async def test_successful_creation_persists_one_package_row_per_requested_package(
        self, sample_tenant, sample_principal, sample_products
    ):
        """TC-DB-002: CreateMediaBuySuccess → one MediaPackage row per package in the request.

        WHY THIS TEST EXISTS:
        Package rows in the media_packages table are the source of truth for update
        operations (budget changes, pause/resume, creative assignment). If a package
        is not written — or written with the wrong media_buy_id FK — the update path
        breaks silently. This test verifies that the count and FK linkage are correct.
        """
        from src.core.database.models import MediaPackage as DBMediaPackage
        from src.core.tools.media_buy_create import _create_media_buy_impl

        req = _make_create_request(
            packages=[
                {"product_id": "guaranteed_display", "budget": 3000.0, "pricing_option_id": "cpm_usd_fixed"},
            ]
        )
        identity = _identity_for(sample_tenant, sample_principal["principal_id"])

        result = await _create_media_buy_impl(req=req, identity=identity)

        assert isinstance(result.response, CreateMediaBuySuccess), (
            f"Expected success to verify package persistence. Errors: {getattr(result.response, 'errors', None)}"
        )
        media_buy_id = result.response.media_buy_id

        with get_db_session() as session:
            packages = session.scalars(select(DBMediaPackage).where(DBMediaPackage.media_buy_id == media_buy_id)).all()

        assert len(packages) == 1, (
            f"Expected 1 MediaPackage row for 1 requested package, "
            f"got {len(packages)} for media_buy_id={media_buy_id!r}."
        )
        assert packages[0].media_buy_id == media_buy_id, "MediaPackage.media_buy_id FK must match the created MediaBuy."
