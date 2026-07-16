"""Shared human-relative time formatting."""

from datetime import UTC, datetime


def relative_time_ago(timestamp: datetime, now: datetime | None = None) -> str:
    """Humanize how long ago ``timestamp`` was: "3d ago", "2h ago", "5m ago", "Just now".

    Naive timestamps are assumed to be UTC. Pass ``now`` to reuse one reference
    instant across a batch of rows.
    """
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    reference = now if now is not None else datetime.now(UTC)
    delta = reference - timestamp
    if delta.days > 0:
        return f"{delta.days}d ago"
    if delta.seconds > 3600:
        return f"{delta.seconds // 3600}h ago"
    if delta.seconds > 60:
        return f"{delta.seconds // 60}m ago"
    return "Just now"
