"""backfill and require products.reporting_capabilities

adcp library 4.4 made ``reporting_capabilities`` a required field on
``Product``. Our column was nullable and most legacy products predate the
field, so wire responses for those tenants emitted no
``reporting_capabilities`` and SDK clients filtered every product to zero
(see issue #71).

This migration:
1. Backfills NULL rows with the minimal-but-spec-valid baseline every
   adapter can honor (daily reporting, impressions metric, UTC,
   date_range support, no webhooks, zero ingestion delay).
2. Adds a server_default so future INSERTs without an explicit value
   still satisfy the constraint.
3. Sets NOT NULL so the schema-level ``default_factory`` shim becomes
   unnecessary — the DB now guarantees the field is present.

Revision ID: c8404b483cf3
Revises: bcd40819d318
Create Date: 2026-05-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8404b483cf3"
down_revision: str | Sequence[str] | None = "bcd40819d318"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SERVER_DEFAULT = (
    "{"
    '"available_reporting_frequencies": ["daily"], '
    '"expected_delay_minutes": 0, '
    '"timezone": "UTC", '
    '"supports_webhooks": false, '
    '"available_metrics": ["impressions"], '
    '"date_range_support": "date_range"'
    "}"
)


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE products
            SET reporting_capabilities = CAST(:default AS jsonb)
            WHERE reporting_capabilities IS NULL
            """
        ),
        {"default": SERVER_DEFAULT},
    )

    op.alter_column(
        "products",
        "reporting_capabilities",
        server_default=sa.text(f"'{SERVER_DEFAULT}'::jsonb"),
        existing_type=sa.dialects.postgresql.JSONB(),
    )
    op.alter_column(
        "products",
        "reporting_capabilities",
        nullable=False,
        existing_type=sa.dialects.postgresql.JSONB(),
        existing_server_default=sa.text(f"'{SERVER_DEFAULT}'::jsonb"),
    )


def downgrade() -> None:
    op.alter_column(
        "products",
        "reporting_capabilities",
        nullable=True,
        server_default=None,
        existing_type=sa.dialects.postgresql.JSONB(),
    )
