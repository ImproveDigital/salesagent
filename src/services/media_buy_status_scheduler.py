"""Media Buy Status Scheduler - Automatically transitions media buy statuses.

This scheduler runs in the background and updates media buy statuses based on
their flight dates:
- pending_activation -> active (when start_time has passed and creatives approved)
- scheduled -> active (when start_time has passed)
- active -> completed (when end_time has passed)
- pending_approval / pending_ad_server_approval -> canceled (when end_time has
  passed before approval landed; the open approval workflow step is closed)

This ensures media buys don't get stuck in transitional states when approved
before their start date.
"""

import asyncio
import logging
import os
from datetime import UTC, date, datetime
from typing import cast

from sqlalchemy import select

from src.core.database.database_session import get_db_session
from src.core.database.models import Creative, CreativeAssignment, MediaBuy
from src.core.database.repositories import MediaBuyRepository
from src.core.database.repositories.workflow import WorkflowRepository
from src.core.helpers.media_buy_flight import APPROVAL_PENDING_STATUSES

logger = logging.getLogger(__name__)

# Recorded on approval workflow steps closed because the flight window ended.
EXPIRED_APPROVAL_MESSAGE = "Flight window ended before the media buy was approved; approval request expired."

# Configurable via env var - default 60 seconds
STATUS_CHECK_INTERVAL_SECONDS = int(os.getenv("MEDIA_BUY_STATUS_CHECK_INTERVAL") or "60")


class MediaBuyStatusScheduler:
    """Scheduler for updating media buy statuses based on flight dates."""

    def __init__(self) -> None:
        self.is_running = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the scheduler background task."""
        async with self._lock:
            if self.is_running:
                logger.warning("Media buy status scheduler is already running")
                return

            self.is_running = True
            self._task = asyncio.create_task(self._run_scheduler())
            logger.info(f"Media buy status scheduler started (checking every {STATUS_CHECK_INTERVAL_SECONDS}s)")

    async def stop(self) -> None:
        """Stop the scheduler background task."""
        async with self._lock:
            if not self.is_running:
                return

            self.is_running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            logger.info("Media buy status scheduler stopped")

    async def _run_scheduler(self) -> None:
        """Main scheduler loop - runs on a fixed cadence."""
        while self.is_running:
            try:
                await self._update_statuses()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in media buy status scheduler: {e}", exc_info=True)
            finally:
                # Wait before next check
                await asyncio.sleep(STATUS_CHECK_INTERVAL_SECONDS)

    async def _update_statuses(self) -> None:
        """Check and update media buy statuses based on flight dates."""
        now = datetime.now(UTC)
        updated_count = 0

        try:
            with get_db_session() as session:
                # Find media buys that need status updates (cross-tenant scheduler query)
                # 1. pending_activation or scheduled -> should become active if start_time passed
                # 2. active -> should become completed if end_time passed
                # 3. approval-pending -> should become canceled if end_time passed
                media_buys = MediaBuyRepository.get_all_by_statuses(
                    session, ["pending_activation", "scheduled", "active", *sorted(APPROVAL_PENDING_STATUSES)]
                )

                for media_buy in media_buys:
                    new_status = self._compute_new_status(media_buy, now, session)

                    if new_status and new_status != media_buy.status:
                        old_status = media_buy.status
                        media_buy.status = new_status
                        if new_status == "canceled":
                            self._expire_approval_steps(media_buy, session, now)
                        updated_count += 1
                        logger.info(f"Updated media buy {media_buy.media_buy_id} status: {old_status} -> {new_status}")

                if updated_count > 0:
                    session.commit()
                    logger.info(f"Updated {updated_count} media buy status(es)")

        except Exception as e:
            logger.error(f"Failed to update media buy statuses: {e}", exc_info=True)

    def _compute_new_status(self, media_buy: MediaBuy, now: datetime, session) -> str | None:
        """Compute the new status for a media buy based on flight dates.

        Returns:
            New status string if change needed, None otherwise.
        """
        # Get start and end times (prefer start_time/end_time over start_date/end_date)
        start_time: datetime | None = None
        if media_buy.start_time:
            raw_start: datetime = media_buy.start_time
            if raw_start.tzinfo is None:
                start_time = raw_start.replace(tzinfo=UTC)
            else:
                start_time = raw_start
        else:
            start_date = cast(date | None, media_buy.start_date)
            if start_date is not None:
                start_time = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=UTC)

        if start_time is None:
            return None  # No start time defined

        end_time: datetime | None = None
        if media_buy.end_time:
            raw_end: datetime = media_buy.end_time
            if raw_end.tzinfo is None:
                end_time = raw_end.replace(tzinfo=UTC)
            else:
                end_time = raw_end
        else:
            end_date = cast(date | None, media_buy.end_date)
            if end_date is not None:
                end_time = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=UTC)

        if end_time is None:
            return None  # No end time defined

        current_status = media_buy.status

        # Awaiting approval: nothing is delivering, so the only date-driven
        # transition is expiry once the flight window has closed.
        if current_status in APPROVAL_PENDING_STATUSES:
            return "canceled" if now > end_time else None

        # Check if campaign has ended
        if now > end_time:
            if current_status != "completed":
                return "completed"
            return None

        # Check if campaign should be active
        if now >= start_time:
            if current_status in ["pending_activation", "scheduled"]:
                # Before activating, verify creatives are approved (for pending_activation)
                if current_status == "pending_activation":
                    if self._are_creatives_approved(media_buy, session):
                        return "active"
                    # Creatives not approved yet - stay pending
                    return None
                else:
                    # scheduled -> active (no creative check needed, already validated)
                    return "active"

        return None

    def _expire_approval_steps(self, media_buy: MediaBuy, session, now: datetime) -> None:
        """Close approval steps left open on a buy whose flight ended unapproved.

        Keeps the admin detail page / workflow queue from offering an Approve
        action that could push an already-expired order to the ad server.
        """
        repo = WorkflowRepository(session, media_buy.tenant_id)
        for step in repo.list_open_approval_steps_for_object("media_buy", media_buy.media_buy_id):
            repo.update_status(step.step_id, status="failed", completed_at=now, error_message=EXPIRED_APPROVAL_MESSAGE)

    def _are_creatives_approved(self, media_buy: MediaBuy, session) -> bool:
        """Check if all creatives for a media buy are approved.

        Returns:
            True if no creatives assigned OR all creatives are approved.
        """
        # Get creative assignments for this media buy
        stmt = select(CreativeAssignment).filter_by(tenant_id=media_buy.tenant_id, media_buy_id=media_buy.media_buy_id)
        assignments = session.scalars(stmt).all()

        if not assignments:
            # No creatives assigned - can activate (some campaigns run without creatives initially)
            return True

        # Get all creative IDs
        creative_ids = list({a.creative_id for a in assignments})

        # Check creative statuses
        creative_stmt = select(Creative).where(
            Creative.tenant_id == media_buy.tenant_id,
            Creative.creative_id.in_(creative_ids),
        )
        creatives = session.scalars(creative_stmt).all()

        # All creatives must be approved
        for creative in creatives:
            if creative.status != "approved":
                return False

        return True


# Global singleton instance
_scheduler: MediaBuyStatusScheduler | None = None


def get_media_buy_status_scheduler() -> MediaBuyStatusScheduler:
    """Get or create the global media buy status scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = MediaBuyStatusScheduler()
    return _scheduler


async def start_media_buy_status_scheduler() -> None:
    """Start the global media buy status scheduler."""
    scheduler = get_media_buy_status_scheduler()
    await scheduler.start()


async def stop_media_buy_status_scheduler() -> None:
    """Stop the global media buy status scheduler."""
    scheduler = get_media_buy_status_scheduler()
    await scheduler.stop()
