"""Improve Digital line items are created Active.

Marketplace doc, "Line item level" → Basic settings → Status: Active means
"if ready (complete) the LI will be sent to the adserver"; Inactive means it
is never sent even when ready. Readiness (inventory + creatives) already
gates serving, so a booked package must not be parked Inactive, which would
leave the campaign silent until someone resumes it by hand.
"""

from contextlib import ExitStack
from unittest.mock import patch

from tests.helpers.adapter_test_helpers import invoke_create_media_buy, make_sample_create_request
from tests.unit.test_improvedigital_live_paths import make_live_adapter, make_targeted_package


def test_booked_line_items_are_created_active():
    adapter = make_live_adapter()

    with ExitStack() as stack:
        # Branches with office-per-currency booking resolve the office from
        # the database; keep this test free of DB access on either branch.
        if hasattr(adapter, "_office_for_currency"):
            stack.enter_context(patch.object(adapter, "_office_for_currency", return_value=635))
        invoke_create_media_buy(adapter, make_sample_create_request(), [make_targeted_package()])

    line_item_payloads = [call[2] for call in adapter._client.campaigns.calls if call[0] == "create_line_item"]
    assert line_item_payloads, adapter._client.campaigns.calls
    assert {p["line_item_status"] for p in line_item_payloads} == {"Active"}
