"""Integration tests for create_media_buy creative validation against real PostgreSQL.

These tests call _validate_creatives_before_adapter_call directly with a real
database session and real Creative rows. The unit tests in
test_create_media_buy_behavioral.py cover the same code with mocked sessions.
These integration tests prove that:

  - The actual SQL query against the creatives table returns the row
  - The status field on the real ORM model is checked correctly
  - The format compatibility check reads from the real products table

Covered gaps:
  TC-CREA-001 — creative with status='error' in real DB → INVALID_CREATIVES
  TC-CREA-002 — creative with status='rejected' in real DB → INVALID_CREATIVES
  TC-CREA-009 — creative format not in product's accepted format_ids → mismatch error
  TC-CREA-010 — no creative_ids on any package → validation is a no-op
"""

from __future__ import annotations

import pytest

from src.core.database.database_session import get_db_session
from src.core.exceptions import AdCPValidationError

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_package_with_creative(creative_id: str, product_id: str = "nonexistent_prod") -> MediaPackage:  # noqa: F821
    """Build a minimal MediaPackage that references a creative_id.

    product_id is set to a non-existent value so the product format-compatibility
    check is skipped (the product isn't in the DB, so product_format_map.get()
    returns None and the loop continues). This isolates the creative status test
    from needing product setup.
    """
    from src.core.schemas import MediaPackage

    return MediaPackage(
        package_id="pkg_test",
        product_id=product_id,
        name="Test Package",
        delivery_type="guaranteed",
        impressions=0,
        format_ids=[],
        creative_ids=[creative_id],
    )


def _create_tenant_and_creative(session, *, status: str, format_id: str = "display_300x250") -> tuple[str, str]:
    """Create a minimal Tenant + Principal + Creative in the DB. Returns (tenant_id, creative_id)."""
    import uuid
    from datetime import UTC, datetime

    from src.core.database.models import Creative, Principal, Tenant

    suffix = uuid.uuid4().hex[:8]
    tenant_id = f"crea_val_{suffix}"
    principal_id = f"agent_{suffix}"
    creative_id = f"cre_{suffix}"
    now = datetime.now(UTC)

    session.add(
        Tenant(
            tenant_id=tenant_id,
            name=f"Creative Val Tenant {suffix}",
            subdomain=f"crea-{suffix}",
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
            platform_mappings={"mock": {"id": "adv_test"}},  # non-empty: PlatformMappingModel requires it
            created_at=now,
        )
    )
    session.flush()  # satisfy FK before inserting Creative
    session.add(
        Creative(
            tenant_id=tenant_id,
            creative_id=creative_id,
            principal_id=principal_id,
            name=f"Test Creative {suffix}",
            agent_url="https://creative.adcontextprotocol.org",
            format=format_id,
            status=status,
            data={"assets": {"banner": {"url": "https://example.com/banner.png"}}},
        )
    )
    session.commit()
    return tenant_id, creative_id


# ===========================================================================
# TC-CREA-001  status = "error"
# ===========================================================================


class TestCreativeErrorStatusBlocksCreate:
    """TC-CREA-001: a creative in 'error' state is rejected before the adapter is called.

    WHY THIS TEST EXISTS:
    The unit test for this (test_creative_error_state_rejected in
    test_create_media_buy_behavioral.py) mocks the session and returns a fake
    Creative object. This integration test proves that the real SQL query returns
    the correct row, the ORM status field is populated, and the validation guard
    fires against a real DB creative — not just against a MagicMock.
    """

    def test_error_status_creative_raises_invalid_creatives(self, integration_db):
        """TC-CREA-001: Creative.status='error' in real DB → AdCPValidationError(INVALID_CREATIVES)."""
        from src.core.tools.media_buy_create import _validate_creatives_before_adapter_call

        with get_db_session() as session:
            tenant_id, creative_id = _create_tenant_and_creative(session, status="error")

        package = _make_package_with_creative(creative_id)

        with get_db_session() as session:
            with pytest.raises(AdCPValidationError) as exc_info:
                _validate_creatives_before_adapter_call([package], tenant_id, session=session)

        error_text = str(exc_info.value)
        assert "error" in error_text.lower(), "Error message must mention the creative's terminal status."
        assert creative_id in error_text, "Error message must name the offending creative_id so buyers can identify it."
        details = exc_info.value.details or {}
        assert details.get("error_code") == "INVALID_CREATIVES", (
            "error_code must be INVALID_CREATIVES for adapter to map it correctly."
        )


# ===========================================================================
# TC-CREA-002  status = "rejected"
# ===========================================================================


class TestCreativeRejectedStatusBlocksCreate:
    """TC-CREA-002: a creative in 'rejected' state is rejected before the adapter.

    WHY THIS TEST EXISTS:
    Same rationale as TC-CREA-001. 'rejected' is a separate terminal state from
    'error' (rejected = human reviewer denied it, error = processing failure). Both
    must be caught. Checking only 'error' would allow rejected creatives to slip through
    and be sent to the ad server, which may then reject them at a more expensive point.
    """

    def test_rejected_status_creative_raises_invalid_creatives(self, integration_db):
        """TC-CREA-002: Creative.status='rejected' in real DB → AdCPValidationError(INVALID_CREATIVES)."""
        from src.core.tools.media_buy_create import _validate_creatives_before_adapter_call

        with get_db_session() as session:
            tenant_id, creative_id = _create_tenant_and_creative(session, status="rejected")

        package = _make_package_with_creative(creative_id)

        with get_db_session() as session:
            with pytest.raises(AdCPValidationError) as exc_info:
                _validate_creatives_before_adapter_call([package], tenant_id, session=session)

        error_text = str(exc_info.value)
        assert "rejected" in error_text.lower(), (
            "Error message must mention 'rejected' so buyers know which review outcome blocked them."
        )
        assert creative_id in error_text


# ===========================================================================
# TC-CREA-009  format mismatch with product
# ===========================================================================


class TestCreativeFormatMismatchBlocksCreate:
    """TC-CREA-009: creative format not accepted by the product → mismatch error.

    WHY THIS TEST EXISTS:
    A creative might be valid on its own (correct URL, dimensions, approved status)
    but incompatible with the product it is being assigned to. For example, a 300x250
    display creative cannot serve on a product that only accepts video formats.
    This test proves the real product format_ids are read from the DB and compared
    against the creative's format, not just that the check code exists.
    """

    def test_format_mismatch_between_creative_and_product_raises_error(self, integration_db):
        """TC-CREA-009: creative format 'display_300x250' on product accepting only 'video_15s' → error."""
        import uuid
        from datetime import UTC, datetime

        from src.core.database.models import Creative, Principal, Product, Tenant
        from src.core.tools.media_buy_create import _validate_creatives_before_adapter_call

        suffix = uuid.uuid4().hex[:8]
        tenant_id = f"fmt_mismatch_{suffix}"
        principal_id = f"agent_{suffix}"
        creative_id = f"cre_{suffix}"
        product_id = f"prod_{suffix}"
        now = datetime.now(UTC)

        with get_db_session() as session:
            session.add(
                Tenant(
                    tenant_id=tenant_id,
                    name=f"Fmt Mismatch {suffix}",
                    subdomain=f"fmt-{suffix}",
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
                    name="Agent",
                    access_token=f"tok_{suffix}",
                    platform_mappings={"mock": {"id": "adv_test"}},
                    created_at=now,
                )
            )
            session.flush()  # satisfy FK before inserting Creative
            # Creative uses display format
            session.add(
                Creative(
                    tenant_id=tenant_id,
                    creative_id=creative_id,
                    principal_id=principal_id,
                    name="Display Creative",
                    agent_url="https://creative.adcontextprotocol.org",
                    format="display_300x250",  # ← display format
                    status="pending",
                    data={"assets": {"banner": {"url": "https://example.com/banner.png", "width": 300, "height": 250}}},
                )
            )
            # Product ONLY accepts video — intentional mismatch
            session.add(
                Product(
                    tenant_id=tenant_id,
                    product_id=product_id,
                    name="Video Product",
                    delivery_type="guaranteed",
                    targeting_template={},
                    format_ids=[{"agent_url": "https://creative.adcontextprotocol.org", "id": "video_15s"}],
                    property_tags=["all_inventory"],
                )
            )
            session.commit()

        # Package references the display creative but targets the video product
        from src.core.schemas import MediaPackage

        package = MediaPackage(
            package_id="pkg_mismatch",
            product_id=product_id,
            name="Test Package",
            delivery_type="guaranteed",
            impressions=0,
            format_ids=[],
            creative_ids=[creative_id],
        )

        with get_db_session() as session:
            with pytest.raises(AdCPValidationError) as exc_info:
                _validate_creatives_before_adapter_call([package], tenant_id, session=session)

        error_text = str(exc_info.value)
        assert creative_id in error_text, "Error must name the creative that caused the mismatch."
        assert product_id in error_text, "Error must name the product whose formats were not matched."
        assert "not accepted" in error_text.lower() or "format" in error_text.lower(), (
            "Error must explain it is a format incompatibility."
        )


# ===========================================================================
# TC-CREA-010  no creative_ids → validation is a no-op
# ===========================================================================


class TestNoCreativesSkipsValidation:
    """TC-CREA-010: packages with no creative_ids → _validate_creatives_before_adapter_call is a no-op.

    WHY THIS TEST EXISTS:
    Most early-stage media buys have no creatives yet (status will be pending_creatives).
    The validation function must return silently in this case — if it raised or queried
    the DB unnecessarily, every no-creative create would pay an extra round-trip cost.
    This test pins the early-return behaviour.
    """

    def test_no_creative_ids_on_packages_returns_silently(self, integration_db):
        """TC-CREA-010: packages with no creative_ids → no exception raised."""
        from src.core.schemas import MediaPackage
        from src.core.tools.media_buy_create import _validate_creatives_before_adapter_call

        package = MediaPackage(
            package_id="pkg_no_creatives",
            product_id="prod_1",
            name="Package without creatives",
            delivery_type="guaranteed",
            impressions=0,
            format_ids=[],
            creative_ids=None,  # no creatives assigned
        )

        with get_db_session() as session:
            # Must not raise — no creative_ids means nothing to validate.
            _validate_creatives_before_adapter_call([package], "any_tenant", session=session)
