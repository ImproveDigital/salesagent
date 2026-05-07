"""Merge reporting_capabilities backfill with billing_enabled flag

Revision ID: 6b31bcf4ebe2
Revises: c8404b483cf3, ee6fe59f5407
Create Date: 2026-05-07 10:22:41.001696

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "6b31bcf4ebe2"
down_revision: Union[str, Sequence[str], None] = ("c8404b483cf3", "ee6fe59f5407")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
