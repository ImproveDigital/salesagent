"""add_improvedigital_line_item_stats

Per-line-item delivery stats cache for the Improve Digital adapter.
Populated by the Report API reporting sync (``POST /report/ext/preview``
with dimensions campaign_id/line_item_id and metrics impressions, clicks,
advertiser_payout, complete). Read by
``ImproveDigitalAdapter.get_media_buy_delivery`` so AdCP delivery surfaces
serve results without round-tripping to 360Yield on every request.

Spend is stored as currency-minor-unit micros (1 EUR = 1_000_000 micros)
to avoid floating-point precision loss when aggregating.

Revision ID: impd02b3c4d5
Revises: impd01a2b3c4
Create Date: 2026-08-18 15:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "impd02b3c4d5"
down_revision: str | Sequence[str] | None = "impd01a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "improvedigital_line_item_stats",
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column(
            "line_item_id",
            sa.String(length=64),
            nullable=False,
            comment="360Yield Classic line-item ID (maps to the AdCP package)",
        ),
        sa.Column(
            "campaign_id",
            sa.String(length=64),
            nullable=True,
            comment="360Yield Classic campaign ID (maps to the AdCP media buy)",
        ),
        sa.Column("impressions", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("clicks", sa.BigInteger(), nullable=True),
        sa.Column(
            "completed_views",
            sa.BigInteger(),
            nullable=True,
            comment="Video completions (Report API 'complete' metric)",
        ),
        sa.Column(
            "spend_micros",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Spend in currency-minor-unit micros (advertiser_payout * 1e6)",
        ),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("delivery_status", sa.String(length=40), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "line_item_id"),
    )
    op.create_index(
        "idx_impd_li_stats_tenant_campaign",
        "improvedigital_line_item_stats",
        ["tenant_id", "campaign_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_impd_li_stats_tenant_campaign", table_name="improvedigital_line_item_stats")
    op.drop_table("improvedigital_line_item_stats")
