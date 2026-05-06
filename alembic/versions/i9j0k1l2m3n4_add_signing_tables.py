"""add signing tables (admitted_operators, links, policy, credentials)

Revision ID: i9j0k1l2m3n4
Revises: h7i8j9k0l1m2
Create Date: 2026-05-05 12:00:00.000000

PR 1 of [signing-non-embedded](../../../docs/design/signing-non-embedded.md).

Operator-trust foundation. Adds:

* ``admitted_operators`` — operators (brand.json publishers) admitted per tenant.
  AAO is consulted at admission time; the brand_json_url points at the operator's
  own house_domain (NOT AAO). Cryptographic trust roots through the brand.json,
  per the AdCP spec.
* ``operator_advertiser_link`` — join table with billing-mode policy
  (``operator_bills`` / ``agent_billed`` / ``disabled``).
* ``tenant_signing_policy`` — per-tenant master switch + ``required_for`` ops.
* ``tenant_signing_credentials`` — KMS-backed signing key references for outbound
  signing. Stores the *reference* + cached public JWK (for the admin UI's
  "copy this into your brand.json" view), never private bytes.
* ``principals.bound_operator_id`` — bearer token → operator binding.
* ``tenants.brand_json_url`` — the salesagent operator's own brand.json URL,
  surfaced on ``get_adcp_capabilities → identity.brand_json_url``.

No middleware mounted, no admin UI yet. PR 2 wires the verifier; PR 3 adds the UI.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from src.core.database.json_type import JSONType

revision: str = "i9j0k1l2m3n4"
down_revision: str | Sequence[str] | None = "h7i8j9k0l1m2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admitted_operators",
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("operator_id", sa.String(length=50), nullable=False),
        sa.Column("brand_json_url", sa.String(length=2048), nullable=False),
        sa.Column("aao_member_slug", sa.String(length=200), nullable=True),
        sa.Column("house_domain", sa.String(length=253), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("is_trusted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_resolution_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "operator_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "brand_json_url", name="uq_admitted_operators_brand_json"),
    )
    op.create_index(
        "idx_admitted_operators_active",
        "admitted_operators",
        ["tenant_id", "is_active"],
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "operator_advertiser_link",
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("operator_id", sa.String(length=50), nullable=False),
        sa.Column("principal_id", sa.String(length=50), nullable=False),
        sa.Column(
            "billing_mode",
            sa.String(length=32),
            nullable=False,
            server_default="operator_bills",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "operator_id", "principal_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operator_id"],
            ["admitted_operators.tenant_id", "admitted_operators.operator_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.principal_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "billing_mode IN ('operator_bills', 'agent_billed', 'disabled')",
            name="chk_operator_advertiser_link_billing_mode",
        ),
    )

    op.create_table(
        "tenant_signing_policy",
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("required_for", JSONType(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "covers_digest_policy",
            sa.String(length=16),
            nullable=False,
            server_default="either",
        ),
        sa.Column("max_skew_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("max_window_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "covers_digest_policy IN ('required', 'forbidden', 'either')",
            name="chk_tenant_signing_policy_digest",
        ),
    )

    op.create_table(
        "tenant_signing_credentials",
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("backend", sa.String(length=32), nullable=False),
        sa.Column("backend_ref", sa.String(length=1024), nullable=False),
        sa.Column("public_jwk", JSONType(), nullable=False),
        sa.Column("key_id", sa.String(length=256), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("rotated_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "purpose", "key_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "backend IN ('local_pem', 'gcp_kms', 'aws_kms', 'hashicorp_vault')",
            name="chk_tenant_signing_credentials_backend",
        ),
    )
    op.create_index(
        "idx_tenant_signing_credentials_active",
        "tenant_signing_credentials",
        ["tenant_id", "purpose", "is_active"],
        postgresql_where=sa.text("is_active"),
    )

    op.add_column(
        "principals",
        sa.Column("bound_operator_id", sa.String(length=50), nullable=True),
    )
    op.create_index(
        "idx_principals_bound_operator",
        "principals",
        ["tenant_id", "bound_operator_id"],
    )

    op.add_column(
        "tenants",
        sa.Column("brand_json_url", sa.String(length=2048), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "brand_json_url")
    op.drop_index("idx_principals_bound_operator", table_name="principals")
    op.drop_column("principals", "bound_operator_id")

    op.drop_index(
        "idx_tenant_signing_credentials_active",
        table_name="tenant_signing_credentials",
    )
    op.drop_table("tenant_signing_credentials")

    op.drop_table("tenant_signing_policy")

    op.drop_table("operator_advertiser_link")

    op.drop_index("idx_admitted_operators_active", table_name="admitted_operators")
    op.drop_table("admitted_operators")
