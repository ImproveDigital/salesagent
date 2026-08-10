#!/usr/bin/env python3
"""Configure a tenant for the Improve Digital (360Yield) adapter.

Reads OAuth2 credentials from the environment (never from arguments, so
secrets don't land in shell history) and upserts the tenant's AdapterConfig
row with a Fernet-encrypted ``config_json`` via
``ImproveDigitalConnectionConfig`` — the same round-trip the admin UI uses.

Usage:
    export IMPROVEDIGITAL_CLIENT_ID=...
    export IMPROVEDIGITAL_CLIENT_SECRET=...
    python scripts/ops/configure_improvedigital_tenant.py \
        --tenant default \
        --demand-contact-id 17918 \
        --api-base-url https://api.360yielddev.com \
        [--advertiser-id 123] [--agency-id 456] \
        [--currency EUR] [--timezone Europe/Amsterdam]

Run inside the app container (or with DATABASE_URL pointing at the app DB):
    docker compose exec adcp-server python scripts/ops/configure_improvedigital_tenant.py ...
"""

import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.adapters.improvedigital.schemas import ImproveDigitalConnectionConfig
from src.core.database.database_session import get_db_session
from src.core.database.models import AdapterConfig, Tenant


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", default="default", help="Tenant ID to configure")
    parser.add_argument("--demand-contact-id", type=int, required=True, help="improve_demand_contact_id")
    parser.add_argument("--api-base-url", default="https://api.360yield.com")
    parser.add_argument("--advertiser-id", type=int, default=None, help="default_advertiser_id")
    parser.add_argument("--agency-id", type=int, default=None)
    parser.add_argument(
        "--buying-entity-id",
        type=int,
        required=True,
        help="Buying entity ID (421 = 'Improve Digital Marketplace' on the dev platform)",
    )
    parser.add_argument("--buying-entity-office-id", type=int, default=None, help="Buyer seat (e.g. 5068 on dev)")
    parser.add_argument(
        "--business-unit-id",
        type=int,
        required=True,
        help="Business unit ID for Classic line items (33 = Azerion on the dev platform)",
    )
    parser.add_argument(
        "--buyer-id",
        type=int,
        default=None,
        help="Buyer ID sent on Classic line items (omitted when unset)",
    )
    parser.add_argument("--currency", default="EUR")
    parser.add_argument("--timezone", default="Europe/Amsterdam")
    args = parser.parse_args()

    client_id = os.environ.get("IMPROVEDIGITAL_CLIENT_ID")
    client_secret = os.environ.get("IMPROVEDIGITAL_CLIENT_SECRET")
    if not (client_id and client_secret):
        print("Set IMPROVEDIGITAL_CLIENT_ID and IMPROVEDIGITAL_CLIENT_SECRET in the environment.", file=sys.stderr)
        return 2

    config = ImproveDigitalConnectionConfig(
        client_id=client_id,
        client_secret=client_secret,
        api_base_url=args.api_base_url,
        improve_demand_contact_id=args.demand_contact_id,
        default_advertiser_id=args.advertiser_id,
        agency_id=args.agency_id,
        buying_entity_id=args.buying_entity_id,
        buying_entity_office_id=args.buying_entity_office_id,
        business_unit_id=args.business_unit_id,
        buyer_id=args.buyer_id,
        currency=args.currency,
        timezone=args.timezone,
    )
    # model_dump() runs the field_serializer → client_secret lands encrypted.
    config_json = config.model_dump()

    with get_db_session() as session:
        tenant = session.get(Tenant, args.tenant)
        if tenant is None:
            print(f"Tenant {args.tenant!r} not found.", file=sys.stderr)
            return 1
        row = session.get(AdapterConfig, args.tenant)
        if row is None:
            row = AdapterConfig(tenant_id=args.tenant, adapter_type="improvedigital")
            session.add(row)
        row.adapter_type = "improvedigital"
        row.config_json = config_json
        tenant.ad_server = "improvedigital"
        session.commit()

    print(
        f"Tenant {args.tenant!r} configured for Improve Digital "
        f"(base_url={args.api_base_url}, demand_contact={args.demand_contact_id}, "
        f"advertiser={args.advertiser_id}, currency={args.currency}, tz={args.timezone})."
    )
    print("Secret stored encrypted in adapter_config.config_json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
