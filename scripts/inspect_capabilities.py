"""Print the raw capabilities response that get_adcp_capabilities returns
for a typical tenant + principal. Throwaway script — useful for auditing what
buyers see at the discovery surface.

Run with the agent-db env exported:

    export DATABASE_URL="postgresql://adcp_user:secure_password_change_me@localhost:50002/adcp_test"
    export ADCP_TESTING=true ENCRYPTION_KEY=PEg0SNGQyvzi4Nft-ForSzK8AGXyhRtql1MgoUsfUHk=
    uv run python scripts/inspect_capabilities.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

# Make sure src/ is importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from src.core.database.database_session import get_db_session  # noqa: E402
from src.core.database.models import (  # noqa: E402
    AuthorizedProperty,
    CurrencyLimit,
    Principal,
    PropertyTag,
    Tenant,
    TenantAuthConfig,
)
from src.core.resolved_identity import ResolvedIdentity  # noqa: E402
from src.core.testing_hooks import AdCPTestContext  # noqa: E402
from src.core.tools.capabilities import _get_adcp_capabilities_impl  # noqa: E402

TENANT_ID = "inspect_capabilities_tenant"
PRINCIPAL_ID = "inspect_principal"


def _ensure_seed() -> None:
    """Create the minimum DB rows required for capabilities to render."""
    # Hard guard: never run against anything that doesn't look like a test DB.
    # The seeded rows include static tokens checked into source control —
    # provisioning them in staging/prod would create permanently-attackable
    # tenants.
    db_url = os.environ.get("DATABASE_URL", "")
    is_testing = os.environ.get("ADCP_TESTING", "").lower() == "true"
    if not is_testing or "test" not in db_url.lower():
        raise RuntimeError(
            f"inspect_capabilities.py refuses to run unless ADCP_TESTING=true and "
            f"DATABASE_URL contains 'test' (got DATABASE_URL='{db_url}', "
            f"ADCP_TESTING='{os.environ.get('ADCP_TESTING', '')}'). "
            "This script seeds rows with static tokens that would be a security "
            "issue in any non-test database."
        )

    now = datetime.now(UTC)
    with get_db_session() as session:
        existing = session.scalars(select(Tenant).where(Tenant.tenant_id == TENANT_ID)).first()
        if existing:
            return

        session.add(
            Tenant(
                tenant_id=TENANT_ID,
                name="Capabilities Inspector Tenant",
                subdomain="capinspect",
                is_active=True,
                ad_server="mock",
                auth_setup_mode=False,
                authorized_emails=["test@example.com"],
                authorized_domains=["example.com"],
                human_review_required=False,
                admin_token=f"capinspect_{uuid4().hex[:8]}",  # generated dev stub, not a credential
                created_at=now,
                updated_at=now,
            )
        )
        session.add(CurrencyLimit(tenant_id=TENANT_ID, currency_code="USD"))
        session.add(
            PropertyTag(
                tenant_id=TENANT_ID,
                tag_id="all_inventory",
                name="All",
                description="All inventory",
            )
        )
        session.add(
            AuthorizedProperty(
                tenant_id=TENANT_ID,
                property_id="capinspect_prop",
                property_type="website",
                name="Inspect Property",
                identifiers=[{"type": "domain", "value": "example.com"}],
                publisher_domain="example.com",
                verification_status="verified",
            )
        )
        session.add(
            TenantAuthConfig(
                tenant_id=TENANT_ID,
                oidc_enabled=True,
                oidc_provider="google",
                oidc_discovery_url="https://accounts.google.com/.well-known/openid-configuration",
                oidc_client_id="capinspect_client",
                oidc_scopes="openid email profile",
            )
        )
        session.add(
            Principal(
                tenant_id=TENANT_ID,
                principal_id=PRINCIPAL_ID,
                name="Inspect Principal",
                access_token="inspect_token",
                platform_mappings={"mock": {"id": "inspect"}},
                created_at=now,
            )
        )
        session.commit()


def main() -> None:
    _ensure_seed()

    tenant_dict = {
        "tenant_id": TENANT_ID,
        "name": "Capabilities Inspector Tenant",
        "subdomain": "capinspect",
        "ad_server": "mock",
        "human_review_required": False,
        "brand_manifest_policy": "public",
    }
    identity = ResolvedIdentity(
        principal_id=PRINCIPAL_ID,
        tenant_id=TENANT_ID,
        tenant=tenant_dict,
        protocol="mcp",
        testing_context=AdCPTestContext(test_session_id="cap-inspect"),
    )

    response = _get_adcp_capabilities_impl(req=None, identity=identity)

    # Use mode='json' so enums/AnyUrl/datetime serialize to wire-shape strings.
    print(json.dumps(response.model_dump(mode="json", exclude_none=False), indent=2, default=str))

    # Quick audit summary.
    print("\n--- AUDIT ---", file=sys.stderr)
    print(f"adcp.major_versions: {[mv.root for mv in response.adcp.major_versions]}", file=sys.stderr)
    print(f"supported_protocols: {[p.value for p in response.supported_protocols]}", file=sys.stderr)
    print(
        f"primary_channels: "
        f"{[c.value for c in (response.media_buy.portfolio.primary_channels or [])] if response.media_buy else None}",
        file=sys.stderr,
    )
    print(
        f"publisher_domains: "
        f"{[d.root for d in (response.media_buy.portfolio.publisher_domains or [])] if response.media_buy else None}",
        file=sys.stderr,
    )
    if response.media_buy and response.media_buy.features:
        f = response.media_buy.features
        print(
            f"features: content_standards={f.content_standards} "
            f"inline_creative_management={f.inline_creative_management} "
            f"property_list_filtering={f.property_list_filtering}",
            file=sys.stderr,
        )
    print(
        "reporting_capabilities present in response: "
        f"{hasattr(response, 'reporting') or 'reporting' in response.model_fields}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
