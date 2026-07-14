"""widen media_buys.status to 30 chars

The new internal-only status ``pending_ad_server_approval`` (26 chars —
set after our-side approval while the GAM order is still awaiting
approval on the ad server) does not fit the previous VARCHAR(20).

Revision ID: e8f1a2b3c4d5
Revises: d75c3a94f2b8
Create Date: 2026-07-13

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e8f1a2b3c4d5"
down_revision: str | Sequence[str] | None = "d75c3a94f2b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "media_buys",
        "status",
        existing_type=sa.String(20),
        type_=sa.String(30),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "media_buys",
        "status",
        existing_type=sa.String(30),
        type_=sa.String(20),
        existing_nullable=False,
    )
