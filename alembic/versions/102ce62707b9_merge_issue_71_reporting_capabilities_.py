"""Merge issue-71 reporting_capabilities backfill with kevel drop

Revision ID: 102ce62707b9
Revises: 6b31bcf4ebe2, e9a1c2d3f4b5
Create Date: 2026-05-07 10:57:15.172280

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "102ce62707b9"
down_revision: Union[str, Sequence[str], None] = ("6b31bcf4ebe2", "e9a1c2d3f4b5")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
