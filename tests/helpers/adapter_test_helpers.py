"""Shared test helpers for adapter unit tests.

Adapter-level tests all need to invoke ``create_media_buy`` with the same
boilerplate (request + packages + start/end times + pricing info). Extracted
into a single helper so individual test files can focus on the assertions
that differ (and the duplicate-code guard stays satisfied).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from src.core.schemas import CreateMediaBuyRequest, FormatId, MediaPackage


def make_sample_create_request() -> CreateMediaBuyRequest:
    """Build a representative AdCP ``CreateMediaBuyRequest`` for adapter tests."""
    from tests.helpers.adcp_factories import create_test_package_request

    start = datetime.now(UTC)
    return CreateMediaBuyRequest(
        idempotency_key=f"adapter-test-{uuid.uuid4()}",
        brand={"domain": "brand.example.com"},
        packages=[create_test_package_request(product_id="prod_video_1")],
        start_time=start,
        end_time=start + timedelta(days=14),
        po_number="PO-ADAPTER-TEST",
    )


def make_sample_video_package(package_id: str = "pkg_video_1") -> MediaPackage:
    """Build a representative video ``MediaPackage`` for adapter tests."""
    return MediaPackage(
        package_id=package_id,
        name="Pre-roll Bundle",
        delivery_type="guaranteed",
        cpm=10.0,
        impressions=500_000,
        format_ids=[FormatId(agent_url="https://test.com", id="video_15s")],
    )


def invoke_create_media_buy(
    adapter: Any,
    request: Any,
    packages: list[Any],
    package_pricing_info: dict[str, dict[str, Any]] | None = None,
) -> Any:
    """Call ``adapter.create_media_buy()`` with the request's start/end times.

    If ``package_pricing_info`` is omitted, a default fixed-CPM entry is
    synthesized for every package, matching what ``media_buy_create`` produces
    in production.
    """
    if package_pricing_info is None:
        package_pricing_info = {
            pkg.package_id: {
                "pricing_model": "cpm",
                "rate": 10.0,
                "currency": "USD",
                "is_fixed": True,
                "bid_price": None,
            }
            for pkg in packages
        }
    return adapter.create_media_buy(
        request=request,
        packages=packages,
        start_time=request.start_time,
        end_time=request.end_time,
        package_pricing_info=package_pricing_info,
    )
