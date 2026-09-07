"""add adapter_config.config_locked_at

Explicit stored lock state for the adapter-configuration lock
(src/core/database/adapter_config_lock.py): stamped when a tenant's first
inventory sync completes; while non-NULL the ad server configuration is
frozen and only credentials remain editable. Cleared only by platform ops
under super_admin_override.

Backfill: tenants that already synced inventory (a terminal-success
sync_jobs row of sync_type='inventory', or legacy gam_inventory rows from
before sync history existed) are stamped locked at migration time, so the
lock applies retroactively.

Revision ID: a9b8c7d6e5f4
Revises: impd02b3c4d5
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9b8c7d6e5f4"
down_revision: str | Sequence[str] | None = "impd02b3c4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "adapter_config",
        sa.Column(
            "config_locked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Set when the tenant's first inventory sync completed; while non-NULL "
                "the ad server configuration is frozen (only credentials editable)."
            ),
        ),
    )

    op.execute(
        """
        UPDATE adapter_config
        SET config_locked_at = NOW()
        WHERE config_locked_at IS NULL
          AND (
            tenant_id IN (
                SELECT DISTINCT tenant_id FROM sync_jobs
                WHERE sync_type = 'inventory' AND status IN ('completed', 'success')
            )
            OR tenant_id IN (SELECT DISTINCT tenant_id FROM gam_inventory)
          )
        """
    )


def downgrade() -> None:
    op.drop_column("adapter_config", "config_locked_at")
