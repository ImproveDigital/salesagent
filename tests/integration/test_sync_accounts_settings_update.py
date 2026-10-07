"""sync_accounts settings-update mode: entries keyed by ``account`` (AccountReference).

Per the AdCP sync-accounts request schema, an entry may carry ``account``
(seller ``account_id`` or the brand+operator natural key) instead of the
provisioning trio. The seller MUST NOT provision in that mode; it updates the
referenced account with the supplied fields only.

Regression: before the fix, ``_extract_natural_key`` dereferenced
``entry.brand.domain`` and crashed with ``AttributeError`` when ``brand`` was
absent, surfacing to buyers as an opaque "Skill execution failed".
"""

from __future__ import annotations

import pytest

from src.core.exceptions import AdCPAccountNotFoundError
from src.core.schemas.account import ListAccountsRequest, SyncAccountsRequest
from src.core.tools.accounts import _list_accounts_impl
from tests.harness.account_sync import AccountSyncEnv

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _action_value(action):
    return action.value if hasattr(action, "value") else str(action)


def _provision(env) -> str:
    req = SyncAccountsRequest(
        accounts=[
            {
                "brand": {"domain": "acme.com"},
                "operator": "example.com",
                "billing": "operator",
                "payment_terms": "net_30",
            }
        ],
    )
    response = env.call_impl(req=req)
    assert _action_value(response.accounts[0].action) == "created"
    return response.accounts[0].account_id


class TestSettingsUpdateMode:
    """Settings-update entries keyed by AccountReference.

    Covers: BR-RULE-056-01
    """

    def test_update_by_account_id_changes_only_supplied_fields(self, integration_db):
        with AccountSyncEnv(tenant_id="su_t1", principal_id="su_agent1") as env:
            env.setup_default_data()
            account_id = _provision(env)

            response = env.call_impl(
                req=SyncAccountsRequest(
                    accounts=[{"account": {"account_id": account_id}, "payment_terms": "net_60"}],
                )
            )

            assert len(response.accounts) == 1
            result = response.accounts[0]
            assert result.account_id == account_id
            assert _action_value(result.action) == "updated"
            # brand/operator are echoed from the stored account (required on the wire)
            assert result.brand.domain == "acme.com"
            assert result.operator == "example.com"

            listed = _list_accounts_impl(ListAccountsRequest(), env.identity)
            stored = next(a for a in listed.accounts if a.account_id == account_id)
            assert stored.payment_terms == "net_60"
            assert stored.billing == "operator"  # absent field must not be cleared

    def test_update_by_natural_key_reference(self, integration_db):
        with AccountSyncEnv(tenant_id="su_t2", principal_id="su_agent2") as env:
            env.setup_default_data()
            account_id = _provision(env)

            response = env.call_impl(
                req=SyncAccountsRequest(
                    accounts=[
                        {
                            "account": {"brand": {"domain": "acme.com"}, "operator": "example.com"},
                            "payment_terms": "net_60",
                        }
                    ],
                )
            )

            result = response.accounts[0]
            assert result.account_id == account_id
            assert _action_value(result.action) == "updated"

    def test_unchanged_when_no_fields_differ(self, integration_db):
        with AccountSyncEnv(tenant_id="su_t3", principal_id="su_agent3") as env:
            env.setup_default_data()
            account_id = _provision(env)

            response = env.call_impl(
                req=SyncAccountsRequest(
                    accounts=[{"account": {"account_id": account_id}, "payment_terms": "net_30"}],
                )
            )

            assert _action_value(response.accounts[0].action) == "unchanged"

    def test_unknown_account_id_is_account_not_found(self, integration_db):
        with AccountSyncEnv(tenant_id="su_t4", principal_id="su_agent4") as env:
            env.setup_default_data()

            with pytest.raises(AdCPAccountNotFoundError):
                env.call_impl(
                    req=SyncAccountsRequest(
                        accounts=[{"account": {"account_id": "acc_does_not_exist"}, "payment_terms": "net_60"}],
                    )
                )
