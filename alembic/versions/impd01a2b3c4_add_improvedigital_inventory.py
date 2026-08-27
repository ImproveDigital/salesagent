"""add_improvedigital_inventory

Local cache of Improve Digital 360Yield buy-side inventory (publishers,
placements, reusable packages, creative sizes). Used by the Improve Digital
adapter's product configuration UI so operators can pick placements from
synced inventory without round-tripping to the 360Yield API on every page
render.

NOT exposed to AdCP buyers -- buyer-facing property discovery goes through
the AAO lookup path (adagents.json + brand.json). This is a private
adapter-side cache.

Refreshed on demand via the adapter settings "Sync Inventory" button.

Revision ID: impd01a2b3c4
Revises: 823974a5553e
Create Date: 2026-08-18 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "impd01a2b3c4"
down_revision: str | Sequence[str] | None = "823974a5553e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create improvedigital_inventory table."""
    op.create_table(
        "improvedigital_inventory",
        sa.Column("tenant_id", sa.String(50), nullable=False),
        sa.Column(
            "entity_type",
            sa.String(40),
            nullable=False,
            comment="360Yield entity kind: publisher, placement, package, size",
        ),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(512), nullable=True),
        sa.Column("parent_id", sa.String(64), nullable=True),
        sa.Column(
            "raw_json",
            sa.JSON().with_variant(postgresql.JSONB, "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "last_synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "entity_type", "entity_id"),
    )
    op.create_index(
        "idx_improvedigital_inventory_tenant_type",
        "improvedigital_inventory",
        ["tenant_id", "entity_type"],
    )
    op.create_index(
        "idx_improvedigital_inventory_parent",
        "improvedigital_inventory",
        ["tenant_id", "parent_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_improvedigital_inventory_parent", table_name="improvedigital_inventory")
    op.drop_index("idx_improvedigital_inventory_tenant_type", table_name="improvedigital_inventory")
    op.drop_table("improvedigital_inventory")
