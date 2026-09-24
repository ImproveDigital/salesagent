"""``get_products`` brief mode with an explicit ``account.account_id`` over the real wire.

Regression: :meth:`core.stores.accounts.SalesagentAccountStore.resolve` returned
the buyer's bare ``acc_...`` id verbatim as ``Account.id``. Brief mode persists a
draft proposal keyed by ``ctx.account.id`` (SDK ``proposal_dispatch`` →
``PgProposalStore.put_draft``), and ``proposals.tenant_id`` is a generated column
``split_part(account_id, ':', 1)`` with an FK to ``tenants``. A bare id yields
``tenant_id = 'acc_...'`` → FK violation → psycopg ``IntegrityError`` → opaque
``INTERNAL_ERROR`` ("Error executing tool get_products") on the wire.

Brief without an account ref (``"<tenant>:default"``), the natural-key ref
``{brand, operator}``, and wholesale (never writes a proposal) were unaffected.
"""

from __future__ import annotations

from typing import Any

import pytest
from adcp.server.auth import current_tenant as auth_current_tenant
from sqlalchemy import text

from core.stores.accounts import SalesagentAccountStore
from src.core.database.database_session import get_db_session
from tests.integration.test_delegate_wire_envelope_cross_transport import (  # noqa: F401  (fixture re-export)
    _call_a2a_raw,
    _call_mcp_raw,
    _extract_a2a_data,
    authenticated_principal,
)

# ``authenticated_principal`` is a pytest fixture re-exported from the cross-transport
# test module; listing it here registers it for this module and keeps F811 quiet.
__all__ = ["authenticated_principal"]

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _brief_payload(authenticated_principal: dict[str, str]) -> dict[str, Any]:
    return {
        "account": {"account_id": authenticated_principal["account_id"]},
        "buying_mode": "brief",
        "brief": "Display banners for a gaming audience",
        "brand": {"domain": "testbrand.example"},
    }


def _proposal_row(proposal_id: str) -> dict[str, Any] | None:
    with get_db_session() as session:
        row = (
            session.execute(
                text("SELECT account_id, tenant_id FROM proposals WHERE proposal_id = :pid"),
                {"pid": proposal_id},
            )
            .mappings()
            .first()
        )
    return dict(row) if row is not None else None


def _assert_brief_response(data: dict[str, Any], authenticated_principal: dict[str, str]) -> None:
    assert "adcp_error" not in data, data
    assert data.get("products"), data
    proposals = data.get("proposals") or []
    assert proposals, f"brief mode must return a proposal for the seeded product; got: {data}"
    proposal_id = proposals[0].get("proposal_id")
    assert proposal_id, proposals[0]

    row = _proposal_row(proposal_id)
    assert row is not None, f"proposal {proposal_id!r} was not persisted"
    # The FK-bearing generated column must resolve to the buyer's tenant, and
    # the persisted key must still be traceable to the buyer's own account id.
    assert row["tenant_id"] == authenticated_principal["tenant_id"], row
    assert row["account_id"].endswith(authenticated_principal["account_id"]), row


class TestBriefModeWithExplicitAccountIdOverTheWire:
    def test_a2a_brief_with_account_id_completes(self, integration_db, authenticated_principal):
        body = _call_a2a_raw("get_products", _brief_payload(authenticated_principal), authenticated_principal)
        data = _extract_a2a_data(body, expected_state="completed")
        _assert_brief_response(data, authenticated_principal)

    def test_mcp_brief_with_account_id_completes(self, integration_db, authenticated_principal):
        result = _call_mcp_raw("get_products", _brief_payload(authenticated_principal), authenticated_principal)
        assert not result.is_error, result
        _assert_brief_response(result.structured_content or {}, authenticated_principal)


class TestAccountStoreComposesTenantIntoExplicitAccountId:
    """The encoding seam: ``Account.id`` must always carry the ``tenant_id:`` prefix
    the proposals schema (and the SDK's multi-tenant guidance) assume."""

    def test_bare_account_id_is_tenant_prefixed(self, integration_db, authenticated_principal):
        tenant_id = authenticated_principal["tenant_id"]
        account_id = authenticated_principal["account_id"]
        token = auth_current_tenant.set(tenant_id)
        try:
            account = SalesagentAccountStore().resolve(ref={"account_id": account_id})
        finally:
            auth_current_tenant.reset(token)

        assert account.metadata == {"tenant_id": tenant_id}
        assert account.id == f"{tenant_id}:{account_id}"

    def test_already_prefixed_account_id_is_kept_verbatim(self, integration_db, authenticated_principal):
        tenant_id = authenticated_principal["tenant_id"]
        token = auth_current_tenant.set(tenant_id)
        try:
            account = SalesagentAccountStore().resolve(ref={"account_id": f"{tenant_id}:acct_demo"})
        finally:
            auth_current_tenant.reset(token)

        assert account.id == f"{tenant_id}:acct_demo"

    def test_missing_account_id_still_mints_default(self, integration_db, authenticated_principal):
        tenant_id = authenticated_principal["tenant_id"]
        token = auth_current_tenant.set(tenant_id)
        try:
            account = SalesagentAccountStore().resolve(ref={"brand": {"domain": "testbrand.example"}})
        finally:
            auth_current_tenant.reset(token)

        assert account.id == f"{tenant_id}:default"
