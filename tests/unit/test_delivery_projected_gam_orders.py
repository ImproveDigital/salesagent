"""get_media_buy_delivery on imported GAM orders that were never claimed.

get_media_buys lists these orders via read-time projection (no MediaBuy
row), so delivery must not report them as "not found".
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import GetMediaBuyDeliveryRequest
from src.core.testing_hooks import AdCPTestContext
from src.core.tools.media_buy_delivery import _get_media_buy_delivery_impl

_MOD = "src.core.tools.media_buy_delivery"


def _run(req: GetMediaBuyDeliveryRequest, unmaterialized: set[str]):
    identity = ResolvedIdentity(
        principal_id="test_principal",
        tenant_id="test_tenant",
        tenant={"tenant_id": "test_tenant"},
        protocol="mcp",
        testing_context=AdCPTestContext(dry_run=False, mock_time=None, jump_to_event=None, test_session_id=None),
    )
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.__exit__.return_value = False
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_MOD}.get_principal_object", return_value=MagicMock()))
        stack.enter_context(patch(f"{_MOD}.get_adapter", return_value=MagicMock()))
        stack.enter_context(patch(f"{_MOD}.MediaBuyUoW", return_value=uow))
        stack.enter_context(patch(f"{_MOD}._get_target_media_buys", return_value=[]))
        stack.enter_context(patch(f"{_MOD}._get_pricing_options", return_value={}))
        projected = stack.enter_context(patch(f"{_MOD}.unmaterialized_projected_ids", return_value=unmaterialized))
        return _get_media_buy_delivery_impl(req, identity), projected


def test_unclaimed_gam_order_reports_not_materialized():
    req = GetMediaBuyDeliveryRequest(media_buy_ids=["gam_4201398719", "mb_missing"])

    response, projected = _run(req, unmaterialized={"gam_4201398719"})

    assert projected.call_args.args[3] == ["gam_4201398719", "mb_missing"]
    assert [(e.code, "gam_4201398719" in e.message) for e in response.errors] == [
        ("media_buy_not_materialized", True),
        ("media_buy_not_found", False),
    ]
    assert "update_media_buy" in response.errors[0].message


def test_unknown_gam_id_still_not_found():
    response, _ = _run(GetMediaBuyDeliveryRequest(media_buy_ids=["gam_999"]), unmaterialized=set())

    assert [e.code for e in response.errors] == ["media_buy_not_found"]
