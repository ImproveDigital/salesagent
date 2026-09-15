"""Live Improve Digital (360Yield) API smoke test.

Exercises the whole Classic client stack — OAuth2 client-credentials mint,
inventory reads, and the full booking write-cleanup cycle (campaign →
line item → placement assignment → creative → binding → pause/resume →
report preview → delete) — against the real Marketplace API. Skipped by
default; runs only when the env vars are set:

    IMPROVEDIGITAL_TEST_CLIENT_ID
    IMPROVEDIGITAL_TEST_CLIENT_SECRET
    IMPROVEDIGITAL_TEST_DEMAND_CONTACT_ID
    IMPROVEDIGITAL_TEST_BUYING_ENTITY_ID          (421 = Improve Digital Marketplace on dev)
    IMPROVEDIGITAL_TEST_BUYING_ENTITY_OFFICE_ID   (buyer seat, e.g. 5068 on dev)
    IMPROVEDIGITAL_TEST_BUSINESS_UNIT_ID          (33 = Azerion on dev)
    IMPROVEDIGITAL_TEST_BASE_URL   (optional; defaults to the dev host)

Run with::

    uv run pytest tests/integration/test_improvedigital_live.py -m live -v

This test creates and deletes real entities on the dev platform. Names are
clearly tagged with ``adcp-live-smoke-`` and a random suffix so any orphans
from a failed cleanup are easy to find and reap by hand.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from src.adapters.improvedigital.client import ImproveDigitalClient, ImproveDigitalError

logger = logging.getLogger(__name__)

CLIENT_ID_ENV = "IMPROVEDIGITAL_TEST_CLIENT_ID"
CLIENT_SECRET_ENV = "IMPROVEDIGITAL_TEST_CLIENT_SECRET"
DEMAND_CONTACT_ENV = "IMPROVEDIGITAL_TEST_DEMAND_CONTACT_ID"
BUYING_ENTITY_ENV = "IMPROVEDIGITAL_TEST_BUYING_ENTITY_ID"
BUYING_ENTITY_OFFICE_ENV = "IMPROVEDIGITAL_TEST_BUYING_ENTITY_OFFICE_ID"
BUSINESS_UNIT_ENV = "IMPROVEDIGITAL_TEST_BUSINESS_UNIT_ID"
BASE_URL_ENV = "IMPROVEDIGITAL_TEST_BASE_URL"
DEFAULT_DEV_BASE_URL = "https://api.360yielddev.com"

pytestmark = pytest.mark.live


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set — live Improve Digital test requires real credentials")
    return value


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture(scope="module")
def client() -> ImproveDigitalClient:
    # Creative binding on the dev platform can take >30s — widen the timeout.
    return ImproveDigitalClient(
        client_id=_require_env(CLIENT_ID_ENV),
        client_secret=_require_env(CLIENT_SECRET_ENV),
        base_url=os.environ.get(BASE_URL_ENV, DEFAULT_DEV_BASE_URL),
        timeout=120.0,
    )


@pytest.fixture(scope="module")
def demand_contact_id() -> int:
    return int(_require_env(DEMAND_CONTACT_ENV))


@pytest.fixture(scope="module")
def buying_entity_id() -> int:
    return int(_require_env(BUYING_ENTITY_ENV))


@pytest.fixture(scope="module")
def buying_entity_office_id() -> int:
    return int(_require_env(BUYING_ENTITY_OFFICE_ENV))


@pytest.fixture(scope="module")
def business_unit_id() -> int:
    return int(_require_env(BUSINESS_UNIT_ENV))


@pytest.fixture
def probe_label() -> str:
    return f"adcp-live-smoke-{uuid.uuid4().hex[:8]}"


class TestReadPaths:
    def test_token_mint_and_campaign_read_probe(self, client):
        status, _ = client.probe("GET", "/rtb/v1/classic/campaigns?limit=1")
        assert status == 200

    def test_placement_search_returns_rows(self, client):
        listing = client.inventory.search_placements(limit=2, offset=0)
        placements = listing.get("placements") or listing.get("content") or []
        assert placements, f"placement search returned no rows: {listing.keys()}"

    def test_sizes_lookup(self, client):
        sizes = client.lookups.sizes()
        assert sizes


class TestBookingCycle:
    def test_full_classic_booking_cycle(
        self, client, demand_contact_id, buying_entity_id, buying_entity_office_id, business_unit_id, probe_label
    ):
        """campaign → line item → placement assign → creative → bind →
        pause/resume → report preview → delete."""
        now = datetime.now(UTC)
        campaign = client.campaigns.create_campaign(
            {
                "name": probe_label,
                "type": "Improve",
                "start_date": _fmt(now + timedelta(minutes=10)),
                "end_date": _fmt(now + timedelta(days=7)),
                "time_zone": "Europe/Amsterdam",
                "currency": "EUR",
                "improve_demand_contact_id": demand_contact_id,
                "buying_entity_id": buying_entity_id,
                "buying_entity_office_id": buying_entity_office_id,
                "buying_entity_office_ids": [buying_entity_office_id],
            }
        )
        campaign_id = int(campaign["id"])
        logger.info("live smoke: created campaign %s", campaign_id)

        try:
            line_item = client.campaigns.create_line_item(
                campaign_id,
                {
                    "name": f"{probe_label}-li",
                    "type": "Standard",
                    "line_item_status": "Active",
                    "goal": "IMPRESSION",
                    "start_date": _fmt(now + timedelta(minutes=10)),
                    "end_date": _fmt(now + timedelta(days=7)),
                    "pricing_model": "CPM",
                    "cpm_bid": 1.0,
                    "impression_cap": 1000,
                    "improve_demand_contact_id": demand_contact_id,
                    "business_unit_id": business_unit_id,
                },
            )
            line_item_id = int(line_item["id"])
            logger.info("live smoke: created line item %s", line_item_id)

            # size_id 4 = 300x250 — match the creative below so the platform's
            # "available placements with this creative size" check passes.
            listing = client.inventory.search_placements(limit=1, offset=0, size_ids=4)
            placements = listing.get("placements") or listing.get("content") or []
            if placements:
                placement_id = int(placements[0].get("placement_id", placements[0].get("id")))
                client.campaigns.set_line_item_placements(
                    campaign_id,
                    line_item_id,
                    {"line_item_placements": [{"id": placement_id, "assigned": True}]},
                )
                logger.info("live smoke: assigned placement %s", placement_id)

            creative = client.creatives.create_third_party_tag_creatives(
                campaign_id,
                [
                    {
                        "name": f"{probe_label}-cr",
                        "size": "300x250 (Medium Rectangle)",
                        "size_id": 4,
                        "width": 300,
                        "height": 250,
                        "status": "Active",
                        "tag": "<div>adcp live smoke</div>",
                        "advertiser_domain": "example.com",
                        "tag_secure": True,
                        "third_party_type": "display",
                        "platform_types": ["Web"],
                    }
                ],
            )
            creative_id = int(creative[0]["id"]) if isinstance(creative, list) else int(creative["creatives"][0]["id"])
            logger.info("live smoke: created creative %s", creative_id)

            client.creatives.set_line_item_creatives(
                campaign_id,
                line_item_id,
                {"line_item_creatives": [{"id": creative_id, "assigned": True}]},
            )

            client.campaigns.set_line_item_status(campaign_id, line_item_id, active=False)
            client.campaigns.set_line_item_status(campaign_id, line_item_id, active=True)

            fetched = client.campaigns.get_campaign(campaign_id)
            assert int(fetched["id"]) == campaign_id

            try:
                preview = client.reporting.preview(
                    {
                        "rows": 50,
                        "report_generation_request": {
                            "title": "",
                            "report_type": "EXT_CONSOLIDATE",
                            "currency_id": 1,
                            "date_range": {"quick": "LAST_7_DAYS"},
                            "dimensions": ["campaign_id", "line_item_id"],
                            "metrics": ["impressions", "clicks", "advertiser_payout", "complete"],
                            "filters": [{"column": "campaign_id", "operation": "IN", "value": [campaign_id]}],
                            "timezone": "UTC",
                            "action": "PREVIEW_REPORT",
                        },
                    }
                )
                assert "rows" in preview
                logger.info("live smoke: report preview returned %d rows", len(preview.get("rows") or []))
            except ImproveDigitalError as exc:
                # A fresh campaign has no delivery yet; only scope errors are
                # worth failing loudly here — everything else already proved
                # the wire shape.
                logger.warning("live smoke: report preview rejected: %s", exc)
                raise
        finally:
            # The dev platform attributes simulated impressions almost
            # immediately, which blocks hard deletes — archive is the
            # platform-sanctioned cleanup for served campaigns.
            try:
                client.campaigns.delete_campaign(campaign_id)
                logger.info("live smoke: deleted campaign %s", campaign_id)
            except ImproveDigitalError:
                client.campaigns.archive_campaign(campaign_id)
                logger.info("live smoke: archived campaign %s (delete blocked by served impressions)", campaign_id)
