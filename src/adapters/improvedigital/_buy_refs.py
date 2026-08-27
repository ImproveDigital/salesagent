"""Resolve Classic campaign IDs from core media-buy rows.

The adapter returns ``improvedigital_<campaign_id>`` as the buy reference at
create time. Depending on the booking path, callers later hold either that
adapter reference directly (``media_buys.media_buy_id`` on the synchronous
path) or the core layer's internal ``mb_*`` ID — in which case the adapter
reference is recovered from ``package_config["platform_order_id"]``, which
the core layer persists on every package of the buy at create time.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

MEDIA_BUY_ID_PREFIX = "improvedigital_"


def campaign_id_from_refs(*refs: Any) -> str | None:
    """First candidate reference that yields a numeric Classic campaign ID.

    Non-numeric candidates (e.g. internal ``mb_*`` IDs) are skipped.
    """
    for ref in refs:
        if not ref:
            continue
        campaign_id = str(ref).removeprefix(MEDIA_BUY_ID_PREFIX)
        if campaign_id.isdigit():
            return campaign_id
    return None


def platform_order_ref(packages: Iterable[Any] | None) -> str | None:
    """The adapter-issued buy reference persisted on the buy's packages.

    ``platform_order_id`` is per-buy — the core layer writes the same value
    to every package, so the first hit wins.
    """
    for package in packages or []:
        ref = (getattr(package, "package_config", None) or {}).get("platform_order_id")
        if ref:
            return str(ref)
    return None
