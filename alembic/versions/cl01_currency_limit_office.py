"""add currency_limits.buying_entity_office_id

Improve Digital books every Classic campaign under a buying-entity office,
and the office fixes the campaign currency (one office per currency). The
office used to be a single tenant-wide value in
adapter_config.config_json["buying_entity_office_id"], which limited a
tenant to one booking currency. It now lives on the currency limit row
(Policies & Workflows → Budget Controls): adding a currency means picking
the office that bills in it, and booking resolves the office from the
buy's currency.

Backfill: the tenant-wide office is copied onto the currency limit row of
the adapter's configured default currency (config_json["currency"], EUR
when unset) so existing tenants keep booking unchanged. The config_json key
is left in place (the connection schema still accepts it as deprecated);
the adapter no longer reads it.

Revision ID: cl01office001
Revises: impd03c4d5e6
Create Date: 2026-10-19

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "cl01office001"
down_revision: str | Sequence[str] | None = "impd03c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "currency_limits",
        sa.Column(
            "buying_entity_office_id",
            sa.BigInteger(),
            nullable=True,
            comment=(
                "Improve Digital buying-entity office that bills in this currency; "
                "Classic campaigns in this currency book under it. NULL for other ad servers."
            ),
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE currency_limits AS cl
            SET buying_entity_office_id = (ac.config_json ->> 'buying_entity_office_id')::bigint
            FROM adapter_config AS ac
            WHERE ac.tenant_id = cl.tenant_id
              AND ac.adapter_type = 'improvedigital'
              AND (ac.config_json ->> 'buying_entity_office_id') ~ '^[0-9]+$'
              AND cl.currency_code = upper(coalesce(nullif(ac.config_json ->> 'currency', ''), 'EUR'))
            """
        )
    )


def downgrade() -> None:
    op.drop_column("currency_limits", "buying_entity_office_id")
