"""Integration tests for create_media_buy persistence details.

Verifies the specific DB artifacts written during a successful create:
  TC-DB-003 — ObjectWorkflowMapping row linking the workflow step to the media_buy_id
  TC-DB-005 — raw_request column stores the full serialized CreateMediaBuyRequest

These cannot be covered by unit tests because:
  - ObjectWorkflowMapping is written in a separate UoW after the main transaction
  - raw_request serialization depends on Pydantic model_dump() and real DB commit

Uses the shared sample_tenant / sample_principal / sample_products fixtures.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from src.core.database.database_session import get_db_session
from src.core.schemas import CreateMediaBuySuccess
from tests.integration.media_buy_helpers import (
    _get_tenant_dict,
    _make_create_request,
    make_lifecycle_identity,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.asyncio]


# ===========================================================================
# TC-DB-003 — ObjectWorkflowMapping persisted
# ===========================================================================


class TestWorkflowMappingPersisted:
    """TC-DB-003: successful create writes an ObjectWorkflowMapping row linking
    the workflow step_id to the media_buy_id.

    WHY THIS TEST EXISTS:
    push_notification_config webhooks depend on ObjectWorkflowMapping rows to know
    which media_buy a completed workflow step refers to. The function
    context_manager._send_push_notifications walks these mappings — without a row,
    it returns early with 'No object mappings found' and the buyer's webhook never fires.

    This is written in a separate UoW (outside the main transaction) in _link_step_to_media_buy.
    Unit tests cannot verify this because they mock the UoW. Only a real DB test
    proves the row is actually committed.
    """

    async def test_workflow_mapping_row_created_after_successful_buy(
        self, sample_tenant, sample_principal, sample_products
    ):
        """TC-DB-003: ObjectWorkflowMapping links workflow step to media_buy_id after create."""
        from src.core.database.models import ObjectWorkflowMapping
        from src.core.tools.media_buy_create import _create_media_buy_impl

        tenant_dict = _get_tenant_dict(sample_tenant["tenant_id"])
        identity = make_lifecycle_identity(tenant_dict, sample_principal["principal_id"])
        req = _make_create_request()

        result = await _create_media_buy_impl(req=req, identity=identity)

        assert isinstance(result.response, CreateMediaBuySuccess), (
            f"Expected success to verify workflow mapping. Errors: {getattr(result.response, 'errors', None)}"
        )
        media_buy_id = result.response.media_buy_id

        with get_db_session() as session:
            mapping = session.scalars(
                select(ObjectWorkflowMapping).where(
                    ObjectWorkflowMapping.object_id == media_buy_id,
                    ObjectWorkflowMapping.object_type == "media_buy",
                    ObjectWorkflowMapping.action == "create",
                )
            ).first()

        assert mapping is not None, (
            f"ObjectWorkflowMapping for media_buy_id={media_buy_id!r} not found in DB. "
            "Without this row, push_notification_config webhooks will not fire on completion."
        )
        assert mapping.step_id is not None, (
            "mapping.step_id must be set — it is the handle the context manager uses "
            "to link the completed workflow step back to this media buy."
        )


# ===========================================================================
# TC-DB-005 — raw_request stored
# ===========================================================================


class TestRawRequestPersisted:
    """TC-DB-005: successful create stores the full CreateMediaBuyRequest as JSON
    in MediaBuy.raw_request.

    WHY THIS TEST EXISTS:
    raw_request is the source of truth used by execute_approved_media_buy when
    re-constructing the CreateMediaBuyRequest after manual approval. If it is
    missing or truncated, approved buys will fail to provision the adapter.

    The unit tests mock the UoW and only check that create_from_request is called
    with the right arguments. This integration test reads the actual stored JSON
    back from the DB and verifies the critical fields are present and correct.
    """

    async def test_raw_request_stored_with_package_and_brand(self, sample_tenant, sample_principal, sample_products):
        """TC-DB-005: MediaBuy.raw_request contains brand, packages, and idempotency_key."""
        from src.core.database.models import MediaBuy as DBMediaBuy
        from src.core.tools.media_buy_create import _create_media_buy_impl

        tenant_dict = _get_tenant_dict(sample_tenant["tenant_id"])
        identity = make_lifecycle_identity(tenant_dict, sample_principal["principal_id"])
        req = _make_create_request()

        result = await _create_media_buy_impl(req=req, identity=identity)

        assert isinstance(result.response, CreateMediaBuySuccess), (
            f"Expected success to verify raw_request. Errors: {getattr(result.response, 'errors', None)}"
        )
        media_buy_id = result.response.media_buy_id

        with get_db_session() as session:
            row = session.scalars(select(DBMediaBuy).where(DBMediaBuy.media_buy_id == media_buy_id)).first()

        assert row is not None, f"MediaBuy row not found for {media_buy_id!r}"
        raw = row.raw_request
        assert raw is not None, "raw_request must be populated — it is required for manual approval reconstruction."
        assert isinstance(raw, dict), "raw_request must be a dict (JSON-parsed)."
        assert "brand" in raw, "raw_request must contain 'brand' — used to reconstruct advertiser context on approval."
        assert "packages" in raw, "raw_request must contain 'packages' — used to reconstruct line items on approval."
        assert "idempotency_key" in raw, (
            "raw_request must contain 'idempotency_key' — needed for idempotency replay logic."
        )
        assert len(raw["packages"]) == 1, (
            "raw_request packages count must match the original request (1 package was sent)."
        )
