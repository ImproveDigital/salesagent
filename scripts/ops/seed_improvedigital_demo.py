#!/usr/bin/env python3
"""Seed everything an MCP buyer needs to book an Improve Digital campaign.

Idempotent — safe to re-run. Creates on the target tenant (default: 'default'):

  - EUR currency limit                      (products need one)
  - 'all_inventory' property tag + a verified authorized property
  - product 'improvedigital_display_300x250' (CPM EUR, five live dev
    placements that accept 300x250) + its pricing option
  - principal 'ci-test-principal' with MCP token 'ci-test-token'
  - approval_mode='auto' so create_media_buy books immediately (no HITL)

Run AFTER configure_improvedigital_tenant.py (which stores the OAuth
credentials). Full local bring-up:

    eval $(.claude/skills/agent-db/agent-db.sh up)      # or any DATABASE_URL
    uv run python scripts/ops/migrate.py
    export IMPROVEDIGITAL_CLIENT_ID=... IMPROVEDIGITAL_CLIENT_SECRET=...
    uv run python scripts/ops/configure_improvedigital_tenant.py \
        --tenant default --demand-contact-id 15663 \
        --api-base-url https://api.360yielddev.com \
        --buying-entity-id 421 --buying-entity-office-id 635 \
        --business-unit-id 33 --timezone Europe/Amsterdam
    uv run python scripts/ops/seed_improvedigital_demo.py
"""

import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import select

from src.core.database.database_session import get_db_session
from src.core.database.models import (
    AuthorizedProperty,
    CurrencyLimit,
    PricingOption,
    Principal,
    Product,
    PropertyTag,
    Tenant,
)

PRODUCT_ID = "improvedigital_display_300x250"
# 300x250-capable placements on the dev platform (size_id 4); refresh via
# GET /rtb/v3/placements?size_ids=4 if these ever disappear.
DEV_PLACEMENT_IDS = [22349458, 22349460, 22349654, 22349657, 22349659]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", default="tenant_azerion_gaming")
    parser.add_argument("--principal-token", default="tok_TsDB5qiLETTMCZab_UAHmUES1cI3tInHt69sxf14TWE", help="MCP x-adcp-auth token to provision")
    args = parser.parse_args()
    tenant_id = args.tenant

    with get_db_session() as s:
        tenant = s.get(Tenant, tenant_id)
        if tenant is None:
            print(f"Tenant {tenant_id!r} not found — run scripts/ops/migrate.py first.", file=sys.stderr)
            return 1

        if not s.get(CurrencyLimit, (tenant_id, "EUR")):
            s.add(CurrencyLimit(tenant_id=tenant_id, currency_code="EUR"))
            print("+ EUR currency limit")

        if not s.get(PropertyTag, ("all_inventory", tenant_id)):
            s.add(
                PropertyTag(
                    tag_id="all_inventory",
                    tenant_id=tenant_id,
                    name="All Inventory",
                    description="All publisher inventory",
                )
            )
            print("+ all_inventory property tag")

        if not s.get(AuthorizedProperty, ("azerion_network", tenant_id)):
            s.add(
                AuthorizedProperty(
                    property_id="azerion_network",
                    tenant_id=tenant_id,
                    property_type="website",
                    name="Azerion 360Yield Network",
                    identifiers=[{"type": "domain", "value": "azerion.com"}],
                    tags=["all_inventory"],
                    publisher_domain="azerion.com",
                    verification_status="verified",
                )
            )
            print("+ authorized property azerion_network")

        if not s.scalars(select(Product).filter_by(tenant_id=tenant_id, product_id=PRODUCT_ID)).first():
            s.add(
                Product(
                    tenant_id=tenant_id,
                    product_id=PRODUCT_ID,
                    name="Improve Digital Display 300x250",
                    description=(
                        "Run-of-network 300x250 display on 360Yield marketplace placements (Improve Digital Classic)."
                    ),
                    format_ids=[{"id": "display_300x250", "agent_url": "https://creative.adcontextprotocol.org"}],
                    targeting_template={},
                    delivery_type="guaranteed",
                    property_tags=["all_inventory"],
                    delivery_measurement={"provider": "improvedigital"},
                    reporting_capabilities={
                        "timezone": "UTC",
                        "available_metrics": ["impressions"],
                        "supports_webhooks": False,
                        "date_range_support": "date_range",
                        "available_reporting_frequencies": ["daily"],
                        "expected_delay_minutes": 0,
                    },
                    implementation_config={
                        "improvedigital": {
                            "placement_ids": DEV_PLACEMENT_IDS,
                            "size_ids": [4],
                            "pricing_model": "CPM",
                        }
                    },
                    property_targeting_allowed=False,
                    signal_targeting_allowed=False,
                )
            )
            s.add(
                PricingOption(
                    tenant_id=tenant_id,
                    product_id=PRODUCT_ID,
                    pricing_model="cpm",
                    rate=2.50,
                    currency="EUR",
                    is_fixed=True,
                )
            )
            print(f"+ product {PRODUCT_ID} (CPM 2.50 EUR, {len(DEV_PLACEMENT_IDS)} placements)")

        if not s.scalars(select(Principal).filter_by(tenant_id=tenant_id, principal_id="prin_960d6568")).first():
            s.add(
                Principal(
                    tenant_id=tenant_id,
                    principal_id="prin_960d6568",
                    name="CI Test Principal",
                    platform_mappings={"improvedigital": {"advertiser_id": None}},
                    access_token=args.principal_token,
                )
            )
            print(f"+ principal ci-test-principal (token: {args.principal_token})")

        if tenant.approval_mode != "auto":
            tenant.approval_mode = "auto"
            tenant.human_review_required = False
            print("+ tenant approval_mode -> auto (no HITL gate on create_media_buy)")

        s.commit()

    print(f"Tenant {tenant_id!r} ready for MCP booking.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
