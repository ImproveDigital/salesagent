"""merge head for audit log signing + gam-lifecycle/auto_naming branches

Revision ID: c1d2e3f4a5b6
Revises: j0k1l2m3n4o5, e77030648663
Create Date: 2026-05-06 13:00:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = ("j0k1l2m3n4o5", "e77030648663")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Merge migration — no schema changes."""
    pass


def downgrade() -> None:
    """Merge migration — no schema changes to revert."""
    pass
