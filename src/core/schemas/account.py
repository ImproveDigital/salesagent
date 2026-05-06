"""Account-related Pydantic schemas.

Extends adcp library account types per pattern #1 (schema inheritance).
All classes are re-exported from ``src.core.schemas`` for backward compatibility.

beads: salesagent-x79
"""

import uuid

from adcp.types import Account as LibraryAccountDomain
from adcp.types import ListAccountsRequest as LibraryListAccountsRequest
from adcp.types import ListAccountsResponse as LibraryListAccountsResponse
from adcp.types import SyncAccountsRequest as LibrarySyncAccountsRequest
from adcp.types.aliases import SyncAccountsSuccessResponse as LibrarySyncAccountsSuccess
from pydantic import ConfigDict, Field

from src.core.config import get_pydantic_extra_mode
from src.core.schemas._base import NestedModelSerializerMixin

# ---------------------------------------------------------------------------
# Core domain Account (used in ListAccountsResponse.accounts)
# ---------------------------------------------------------------------------


class Account(LibraryAccountDomain):
    """Extends library Account with salesagent model_config.

    Library provides: account_id, name, advertiser, billing_proxy, status,
    brand, operator, billing, rate_card, payment_terms, credit_limit, setup,
    account_scope, governance_agents, sandbox, ext.
    """

    model_config = ConfigDict(extra=get_pydantic_extra_mode())


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ListAccountsRequest(LibraryListAccountsRequest):
    """Extends library ListAccountsRequest.

    Library provides: status, pagination, sandbox, context, ext.
    """

    model_config = ConfigDict(extra=get_pydantic_extra_mode())


class SyncAccountsRequest(LibrarySyncAccountsRequest):
    """Extends library SyncAccountsRequest.

    Library provides: accounts, delete_missing, dry_run, idempotency_key,
    push_notification_config, context, ext.

    adcp 4.4.3 made ``idempotency_key`` required. Auto-default to a fresh
    UUID so pre-v3 callers (and most internal tests) keep working without
    minting a key by hand.
    """

    model_config = ConfigDict(extra=get_pydantic_extra_mode())

    idempotency_key: str = Field(
        default_factory=lambda: f"idem_{uuid.uuid4()}",
        description="Client-generated unique key. Auto-defaults to a fresh UUID when omitted.",
        min_length=16,
        max_length=255,
        pattern=r"^[A-Za-z0-9_.:-]{16,255}$",
    )


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ListAccountsResponse(NestedModelSerializerMixin, LibraryListAccountsResponse):
    """Extends library ListAccountsResponse.

    Library provides: accounts, errors, pagination, context, ext.
    NestedModelSerializerMixin ensures nested Account objects serialize correctly.
    """

    model_config = ConfigDict(extra=get_pydantic_extra_mode())

    def __str__(self) -> str:
        """Return human-readable summary message for protocol envelope."""
        count = len(self.accounts) if self.accounts else 0
        return f"Found {count} account{'s' if count != 1 else ''}."


class SyncAccountsResponse(NestedModelSerializerMixin, LibrarySyncAccountsSuccess):
    """Extends library SyncAccountsResponse success variant.

    adcp 3.10: SyncAccountsResponse is a union TypeAlias (not RootModel).
    Since the error variant is never constructed (ToolError handles failures),
    we subclass the success variant directly. Fields (accounts, dry_run,
    context, ext) are inherited.
    """

    model_config = ConfigDict(extra=get_pydantic_extra_mode())

    def __str__(self) -> str:
        """Return human-readable summary message for protocol envelope."""
        count = len(self.accounts) if self.accounts else 0
        dry_run_note = " (dry run)" if self.dry_run else ""
        return f"Synced {count} account{'s' if count != 1 else ''}{dry_run_note}."


__all__ = [
    "Account",
    "ListAccountsRequest",
    "ListAccountsResponse",
    "SyncAccountsRequest",
    "SyncAccountsResponse",
]
