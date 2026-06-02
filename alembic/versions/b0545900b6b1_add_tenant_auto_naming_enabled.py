"""add tenant.auto_naming_enabled

Revision ID: b0545900b6b1
Revises: m3n4o5p6q7r8
Create Date: 2026-05-06 06:39:54.991546

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b0545900b6b1"
down_revision: Union[str, Sequence[str], None] = "m3n4o5p6q7r8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add tenant.auto_naming_enabled boolean column.

    Defaults to True so existing tenants whose order_name_template references
    {auto_name} keep working unchanged. Tenants without a Gemini API key can
    set this to False to silence the warning and skip the AI call entirely.
    """
    op.add_column(
        "tenants",
        sa.Column(
            "auto_naming_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    """Drop tenant.auto_naming_enabled."""
    op.drop_column("tenants", "auto_naming_enabled")
