"""add trigram index for Improve Digital inventory picker search

The product-form pickers page through ``improvedigital_inventory`` with a
case-insensitive substring match on ``name`` OR ``entity_id`` (see
``ImproveDigitalInventoryRepository.list_picker_rows`` /
``count_picker_rows``). Without an index every keystroke is a sequential
scan of the tenant's placement rows — hundreds of thousands in dev. A GIN
trigram index lets PostgreSQL answer ``ILIKE '%term%'`` on both columns
with a bitmap scan.

``pg_trgm`` ships with PostgreSQL (and RDS) and is a *trusted* extension on
PostgreSQL 13+, so the application role can install it. The migration is
defensive anyway: if the extension is unavailable or cannot be created, it
logs a warning and leaves the table unindexed — the search still works,
just slower — rather than failing the boot-time migration and taking the
service down over a performance index.

Revision ID: impd03c4d5e6
Revises: a9b8c7d6e5f4
Create Date: 2026-09-11

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "impd03c4d5e6"
down_revision: str | Sequence[str] | None = "a9b8c7d6e5f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_improvedigital_inventory_trgm"


def _ensure_pg_trgm(conn: sa.Connection) -> bool:
    """Install ``pg_trgm`` if needed. Returns False when it cannot be used."""
    installed = conn.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")).scalar()
    if installed:
        return True
    available = conn.execute(sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm'")).scalar()
    if not available:
        print("WARNING: pg_trgm is not available on this PostgreSQL server; skipping trigram index")
        return False
    # Savepoint so a permission failure does not abort the surrounding
    # migration transaction.
    try:
        with conn.begin_nested():
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    except sa.exc.DBAPIError as exc:
        print(f"WARNING: could not create pg_trgm extension ({exc.orig}); skipping trigram index")
        return False
    return True


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    if not _ensure_pg_trgm(conn):
        return
    conn.execute(
        sa.text(
            f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
            "ON improvedigital_inventory USING gin (name gin_trgm_ops, entity_id gin_trgm_ops)"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    # The extension is left installed: other objects may come to rely on it.
    conn.execute(sa.text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
