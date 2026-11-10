"""FMS-2148: the status scheduler expires buys whose flight ended unapproved.

A buy parked in ``pending_approval`` past its flight end moves to ``canceled``
and its open approval workflow step is closed, so the admin UI no longer
offers an Approve action that would push an expired order to the ad server.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.core.database.models import MediaBuy, WorkflowStep
from src.services.media_buy_status_scheduler import EXPIRED_APPROVAL_MESSAGE, MediaBuyStatusScheduler
from tests.harness._base import IntegrationEnv

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


class _ExpiryEnv(IntegrationEnv):
    EXTERNAL_PATCHES: dict[str, str] = {}

    def get_session(self):
        self._commit_factory_data()
        return self._session


def _seed_pending_buy(media_buy_id: str, end_time: datetime):
    from tests.factories import (
        ContextFactory,
        MediaBuyFactory,
        ObjectWorkflowMappingFactory,
        PrincipalFactory,
        TenantFactory,
        WorkflowStepFactory,
    )

    tenant = TenantFactory(tenant_id="t_expiry")
    principal = PrincipalFactory(tenant=tenant, principal_id="p_expiry")
    MediaBuyFactory(
        tenant=tenant,
        principal=principal,
        media_buy_id=media_buy_id,
        status="pending_approval",
        start_time=end_time - timedelta(days=2),
        end_time=end_time,
        start_date=(end_time - timedelta(days=2)).date(),
        end_date=end_time.date(),
    )
    context = ContextFactory(tenant=tenant, principal=principal)
    step = WorkflowStepFactory(context=context, status="requires_approval")
    ObjectWorkflowMappingFactory(workflow_step=step, object_id=media_buy_id)
    return step.step_id


@pytest.mark.asyncio
async def test_ended_pending_approval_buy_is_canceled_and_step_closed(integration_db):
    with _ExpiryEnv(tenant_id="t_expiry", principal_id="p_expiry") as env:
        step_id = _seed_pending_buy("mb_expired", datetime.now(UTC) - timedelta(days=4))
        session = env.get_session()

        await MediaBuyStatusScheduler()._update_statuses()

        session.expire_all()
        buy = session.scalars(select(MediaBuy).filter_by(media_buy_id="mb_expired")).one()
        step = session.scalars(select(WorkflowStep).filter_by(step_id=step_id)).one()
        assert buy.status == "canceled"
        assert step.status == "failed"
        assert step.error_message == EXPIRED_APPROVAL_MESSAGE


@pytest.mark.asyncio
async def test_in_flight_pending_approval_buy_is_untouched(integration_db):
    with _ExpiryEnv(tenant_id="t_expiry", principal_id="p_expiry") as env:
        step_id = _seed_pending_buy("mb_open", datetime.now(UTC) + timedelta(days=4))
        session = env.get_session()

        await MediaBuyStatusScheduler()._update_statuses()

        session.expire_all()
        buy = session.scalars(select(MediaBuy).filter_by(media_buy_id="mb_open")).one()
        step = session.scalars(select(WorkflowStep).filter_by(step_id=step_id)).one()
        assert buy.status == "pending_approval"
        assert step.status == "requires_approval"
