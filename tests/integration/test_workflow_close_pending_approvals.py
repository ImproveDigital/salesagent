"""WorkflowRepository.close_pending_approvals_for_object against real PostgreSQL.

Canceling a media buy closes the approval steps still open for it, so the
admin stops offering Approve/Reject on a buy that can no longer run.
"""

from __future__ import annotations

import pytest

from src.core.database.repositories.workflow import WorkflowRepository
from tests.factories import (
    ContextFactory,
    ObjectWorkflowMappingFactory,
    TenantFactory,
    WorkflowStepFactory,
)
from tests.helpers.managed_tenant_api import bind_factories_to_session

REASON = "Media buy canceled by the buyer before a decision was made"


def _step(context, media_buy_id: str, **fields):
    step = WorkflowStepFactory(context=context, **fields)
    ObjectWorkflowMappingFactory(workflow_step=step, object_id=media_buy_id)
    return step


@pytest.mark.requires_db
class TestClosePendingApprovalsForObject:
    def test_closes_only_pending_approvals_of_that_buy(self, integration_db):
        with bind_factories_to_session() as session:
            context = ContextFactory()
            create_step = _step(context, "mb_target", status="requires_approval")
            update_step = _step(context, "mb_target", status="requires_approval", tool_name="update_media_buy")
            cancel_step = _step(context, "mb_target", status="requires_approval", tool_name="update_media_buy")
            done_step = _step(context, "mb_target", status="completed")
            other_buy_step = _step(context, "mb_other", status="requires_approval")

            repo = WorkflowRepository(session, context.tenant_id)
            closed = repo.close_pending_approvals_for_object(
                "media_buy", "mb_target", reason=REASON, exclude_step_id=cancel_step.step_id
            )
            session.commit()

            assert sorted(closed) == sorted([create_step.step_id, update_step.step_id])
            for step in (create_step, update_step):
                session.refresh(step)
                assert step.status == "rejected"
                assert step.error_message == REASON
                assert step.completed_at is not None
            for step, expected in ((cancel_step, "requires_approval"), (done_step, "completed")):
                session.refresh(step)
                assert step.status == expected
            session.refresh(other_buy_step)
            assert other_buy_step.status == "requires_approval"

    def test_does_not_touch_another_tenants_steps(self, integration_db):
        with bind_factories_to_session() as session:
            context = ContextFactory()
            step = _step(context, "mb_shared_id", status="requires_approval")
            other_tenant = TenantFactory()

            closed = WorkflowRepository(session, other_tenant.tenant_id).close_pending_approvals_for_object(
                "media_buy", "mb_shared_id", reason=REASON
            )
            session.commit()

            assert closed == []
            session.refresh(step)
            assert step.status == "requires_approval"
