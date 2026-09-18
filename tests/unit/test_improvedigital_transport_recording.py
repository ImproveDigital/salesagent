"""Improve Digital transport: request recording for the create_media_buy audit trail."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.adapters.improvedigital._transport import ImproveDigitalError, ImproveDigitalTransport
from tests.helpers.adapter_test_helpers import stub_http_response


def _transport(session: MagicMock) -> ImproveDigitalTransport:
    transport = ImproveDigitalTransport(
        client_id="app", client_secret="s", base_url="https://api.test", session=session
    )
    transport._token_cache = MagicMock()
    transport._token_cache.current.return_value = "tok"
    return transport


def _ok(body: dict) -> MagicMock:
    raw = json.dumps(body).encode()
    response = stub_http_response(200, content=raw, text=raw.decode())
    response.json.return_value = body
    return response


class TestRecordRequests:
    def test_records_method_path_status_and_parsed_body_inside_block_only(self):
        session = MagicMock()
        session.request.return_value = _ok({"id": 101})
        transport = _transport(session)

        transport.post_json("/rtb/v1/classic/campaigns", {"name": "before"})  # outside: not recorded
        with transport.record_requests() as recorded:
            transport.post_json("/rtb/v1/classic/campaigns", {"name": "adcp_x", "reference_number": "nike"})
            transport.put_json("/rtb/v1/classic/campaigns/101", {"id": 101}, update_mask="name")
            transport.get_json("/rtb/v1/classic/campaigns/101")
        transport.post_json("/rtb/v1/classic/campaigns", {"name": "after"})  # outside: not recorded

        assert [r["method"] for r in recorded] == ["POST", "PUT", "GET"]
        assert recorded[0] == {
            "method": "POST",
            "path": "/rtb/v1/classic/campaigns",
            "status": 200,
            "body": {"name": "adcp_x", "reference_number": "nike"},
        }
        assert recorded[1]["params"] == {"update_mask": "name"}
        assert "body" not in recorded[2]
        # Headers (and so the bearer) are never part of the record.
        assert all("headers" not in r and "tok" not in json.dumps(r) for r in recorded)

    def test_failed_calls_are_recorded_before_raising(self):
        session = MagicMock()
        session.request.return_value = stub_http_response(400, content=b'{"message":"bad"}', text='{"message":"bad"}')
        transport = _transport(session)
        with transport.record_requests() as recorded:
            with pytest.raises(ImproveDigitalError):
                transport.post_json("/rtb/v1/classic/campaigns/1/line-items", {"name": "li"})
        assert recorded[0]["status"] == 400
        assert recorded[0]["body"] == {"name": "li"}

    def test_oversized_bodies_are_truncated(self):
        session = MagicMock()
        session.request.return_value = _ok({})
        transport = _transport(session)
        with transport.record_requests() as recorded:
            transport.post_json("/x", {"blob": "a" * 30_000})
        assert recorded[0]["body_truncated"] is True
        assert len(recorded[0]["body"]) == 20_000

    def test_multipart_records_part_names_not_files(self):
        session = MagicMock()
        session.request.return_value = _ok({"id": 9})
        transport = _transport(session)
        with transport.record_requests() as recorded:
            transport.post_multipart("/rtb/v1/classic/campaigns/1/creatives", {"name": "c"})
        assert recorded[0]["multipart_parts"] == ["body"]
        assert "body" not in recorded[0]
