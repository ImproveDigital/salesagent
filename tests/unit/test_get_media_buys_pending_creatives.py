"""get_media_buys must agree with create_media_buy on ``pending_creatives`` (FMS-2166).

A buy created on a manual-approval tenant is persisted as ``pending_approval``.
``create_media_buy`` (and its idempotent replay) report ``pending_creatives``
when the buy has no creatives; ``get_media_buys`` reported ``pending_start``
for the same buy.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from adcp.types.generated_poc.enums.media_buy_status import MediaBuyStatus

from src.core.schemas import GetMediaBuysRequest
from src.core.tools.media_buy_list import _compute_status, _fetch_target_media_buys

TODAY = date(2026, 11, 17)


def _db_buy(media_buy_id: str, status: str) -> SimpleNamespace:
    return SimpleNamespace(
        media_buy_id=media_buy_id,
        currency="EUR",
        budget=Decimal("1"),
        start_date=date(2026, 12, 1),
        end_date=date(2026, 12, 2),
        start_time=datetime(2026, 12, 1, tzinfo=UTC),
        end_time=datetime(2026, 12, 2, tzinfo=UTC),
        raw_request={},
        created_at=datetime(2026, 11, 17, tzinfo=UTC),
        updated_at=datetime(2026, 11, 17, tzinfo=UTC),
        revision=1,
        approved_at=None,
        confirmed_at=None,
        status=status,
    )


def _uow(buys: list[SimpleNamespace], with_creatives: set[str]) -> MagicMock:
    uow = MagicMock()
    uow.media_buys.get_by_principal.return_value = buys
    uow.media_buys.ids_with_creative_assignments.return_value = with_creatives
    return uow


def _statuses(uow: MagicMock, req: GetMediaBuysRequest) -> dict[str, MediaBuyStatus]:
    return {buy.media_buy_id: _compute_status(buy, TODAY) for buy in _fetch_target_media_buys(req, "p1", uow, TODAY)}


def test_pending_approval_without_creatives_reads_pending_creatives():
    uow = _uow([_db_buy("mb_1", "pending_approval")], with_creatives=set())

    statuses = _statuses(uow, GetMediaBuysRequest(media_buy_ids=["mb_1"]))

    assert statuses == {"mb_1": MediaBuyStatus.pending_creatives}


def test_pending_approval_with_creatives_reads_pending_start():
    uow = _uow([_db_buy("mb_1", "pending_approval")], with_creatives={"mb_1"})

    statuses = _statuses(uow, GetMediaBuysRequest(media_buy_ids=["mb_1"]))

    assert statuses == {"mb_1": MediaBuyStatus.pending_start}


def test_status_filter_matches_reported_status():
    """The filter and the reported status use the same rule."""
    buys = [_db_buy("mb_no_creatives", "pending_approval"), _db_buy("mb_with_creatives", "pending_approval")]
    uow = _uow(buys, with_creatives={"mb_with_creatives"})

    statuses = _statuses(uow, GetMediaBuysRequest(status_filter=MediaBuyStatus.pending_creatives))

    assert statuses == {"mb_no_creatives": MediaBuyStatus.pending_creatives}


def test_ad_server_approval_pending_stays_pending_start():
    """Only our own approval gate can precede creatives; a GAM order awaiting
    ad-server approval was already booked."""
    uow = _uow([_db_buy("mb_1", "pending_ad_server_approval")], with_creatives=set())

    statuses = _statuses(uow, GetMediaBuysRequest(media_buy_ids=["mb_1"]))

    assert statuses == {"mb_1": MediaBuyStatus.pending_start}
