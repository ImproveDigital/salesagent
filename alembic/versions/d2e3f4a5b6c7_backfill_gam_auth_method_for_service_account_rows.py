"""backfill adapter_config.gam_auth_method for service-account rows

Tenants provisioned via the embedded-mode tenant management API
(``src/admin/tenant_management_api.py:_persist_adapter_config``) before the
fix in this PR were inserted with the column server_default
``gam_auth_method='oauth'`` even when only a service-account JSON was
provided (no refresh token). The inventory + custom-targeting sync paths
in ``src/services/background_sync_service.py`` honored
``gam_auth_method`` and therefore tried to build a
``GoogleRefreshTokenClient(refresh_token=None)``, which fails on first GAM
API call with: "The credentials do not contain the necessary fields need
to refresh the access token. You must specify refresh_token, token_uri,
client_id, and client_secret."

This migration repairs the existing rows. New rows are written correctly
by the provisioning fix, and the sync code now detects auth method from
credential presence regardless of ``gam_auth_method``.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-05-06 13:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2e3f4a5b6c7"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Repair rows where SA JSON is present but auth_method says oauth."""
    op.execute(
        sa.text(
            """
            UPDATE adapter_config
               SET gam_auth_method = 'service_account'
             WHERE adapter_type = 'google_ad_manager'
               AND gam_auth_method = 'oauth'
               AND gam_service_account_json IS NOT NULL
               AND (gam_refresh_token IS NULL OR gam_refresh_token = '')
            """
        )
    )


def downgrade() -> None:
    """Backfills are not reversed.

    The upgrade query cannot be inverted unambiguously — rows that were
    always `service_account` are indistinguishable from rows the upgrade
    flipped. The column has a safe server_default of 'oauth' and the runtime
    detects auth method from credential presence (see
    build_gam_config_from_adapter), so leaving repaired rows as
    service_account is harmless if the code is rolled back too.
    """
    pass
