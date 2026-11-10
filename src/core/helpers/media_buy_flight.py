"""Flight-window helpers shared by the scheduler, approval gates and dashboard.

A media buy's flight end is ``end_time`` when set, otherwise the end of the
``end_date`` day (inclusive). Naive datetimes are treated as UTC.
"""

from datetime import UTC, date, datetime
from typing import Any

# Internal statuses where the buy awaits a (publisher or ad-server) approval
# and is therefore not delivering. A flight that ends while parked here can
# never deliver, so it must move to a terminal status.
APPROVAL_PENDING_STATUSES: frozenset[str] = frozenset({"pending_approval", "pending_ad_server_approval"})


def flight_end_utc(media_buy: Any) -> datetime | None:
    """Return the buy's flight end as an aware UTC datetime, or ``None``."""
    end_time = getattr(media_buy, "end_time", None)
    if isinstance(end_time, datetime):
        return end_time.astimezone(UTC) if end_time.tzinfo else end_time.replace(tzinfo=UTC)
    end_date = getattr(media_buy, "end_date", None)
    if isinstance(end_date, date):
        return datetime.combine(end_date, datetime.max.time()).replace(tzinfo=UTC)
    return None


def flight_has_ended(media_buy: Any, now: datetime) -> bool:
    """True when the buy's flight window closed at or before ``now``."""
    end = flight_end_utc(media_buy)
    return end is not None and end <= now
