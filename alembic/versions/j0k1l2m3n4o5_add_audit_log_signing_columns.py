"""add verified-signer columns to audit_logs

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-05-05 13:00:00.000000

PR 2 of [signing-non-embedded](../../../docs/design/signing-non-embedded.md).

Adds three columns to ``audit_logs`` so signed-request verification leaves a
trail tied to the verified operator + agent + key:

* ``verified_operator_id`` — the AdmittedOperator the signature was attributed to.
* ``verified_agent_url`` — the specific agent within the operator's brand.json
  whose JWK matched (carries which agent of the operator signed the request).
* ``verified_key_id`` — the kid from the verified Signature-Input header.

All three are NULL on rows for unsigned requests and on legacy rows. Populated
by ``SigningVerifyMiddleware`` (PR 2's middleware mount).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "j0k1l2m3n4o5"
down_revision: str | Sequence[str] | None = "i9j0k1l2m3n4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_logs",
        sa.Column("verified_operator_id", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("verified_agent_url", sa.String(length=2048), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("verified_key_id", sa.String(length=256), nullable=True),
    )
    op.create_index(
        "idx_audit_logs_verified_operator",
        "audit_logs",
        ["tenant_id", "verified_operator_id"],
        postgresql_where=sa.text("verified_operator_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_audit_logs_verified_operator", table_name="audit_logs")
    op.drop_column("audit_logs", "verified_key_id")
    op.drop_column("audit_logs", "verified_agent_url")
    op.drop_column("audit_logs", "verified_operator_id")
