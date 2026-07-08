"""Unit tests for create_media_buy identity and authentication guards.

These are the very first checks in _create_media_buy_impl, executed before any
database access or business logic. They raise typed exceptions that propagate to
the transport wrapper (MCP/A2A) and are converted to protocol-level auth errors.

Because the guards fire before the try/except that catches ValueError, these tests
use pytest.raises — NOT checking the return value of the function.

Covered gaps (not in any existing test as of this writing):
  TC-AUTH-001 — identity=None raises AdCPValidationError ("Identity is required")
  TC-AUTH-002 — principal_id=None raises AdCPAuthenticationError
  TC-AUTH-003 — tenant=None raises AdCPAuthenticationError

Note: The existing test_authentication_always_required (test_create_media_buy_behavioral.py,
line 2867) tests the full MCP path but uses the _PatchContext harness which provides a
valid identity. These tests call _create_media_buy_impl directly with deliberately broken
identity to pin the exact guard conditions.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.core.exceptions import AdCPAuthenticationError, AdCPValidationError
from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import CreateMediaBuyRequest
from src.core.testing_hooks import AdCPTestContext
from tests.factories.spec_required_kwargs import required_request_kwargs


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _future(days: int = 7) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _minimal_valid_request() -> CreateMediaBuyRequest:
    """Build the smallest valid CreateMediaBuyRequest.

    Has one package so get_total_budget() > 0 and no reporting_webhook
    so the early frequency-warning block is skipped. Identity failures
    happen before any field on the request is inspected beyond reporting_webhook.
    """
    return CreateMediaBuyRequest(
        **required_request_kwargs(),
        brand={"domain": "testbrand.com"},
        start_time=_future(1),
        end_time=_future(8),
        packages=[
            {
                "product_id": "prod_1",
                "budget": 5000.0,
                "pricing_option_id": "cpm_usd_fixed",
            }
        ],
    )


# ===========================================================================
# TC-AUTH-001  identity = None
# ===========================================================================


class TestIdentityNoneGuard:
    """TC-AUTH-001: _create_media_buy_impl raises immediately when identity is None.

    The function signature declares identity as optional (identity=None) because
    the transport wrappers always supply it, but the impl must still guard against
    None so that any caller that forgets to supply it gets a clear typed error
    rather than an AttributeError deep inside business logic.
    """

    @pytest.mark.asyncio
    async def test_none_identity_raises_validation_error(self):
        """TC-AUTH-001: identity=None → AdCPValidationError("Identity is required").

        WHY THIS TEST EXISTS:
        If identity is None, every subsequent line that calls identity.principal_id,
        identity.tenant, or identity.testing_context would raise AttributeError.
        The guard at the top of the function converts this into a typed
        AdCPValidationError so the transport layer can map it to a clean error
        response instead of a 500.
        """
        from src.core.tools.media_buy_create import _create_media_buy_impl

        req = _minimal_valid_request()

        with pytest.raises(AdCPValidationError) as exc_info:
            await _create_media_buy_impl(req=req, identity=None)

        assert "Identity is required" in str(exc_info.value), (
            "The error message must name the problem clearly so the caller can diagnose it."
        )


# ===========================================================================
# TC-AUTH-002  principal_id = None
# ===========================================================================


class TestPrincipalIdNoneGuard:
    """TC-AUTH-002: _create_media_buy_impl raises when principal_id is None.

    A ResolvedIdentity with principal_id=None means the token was valid enough to
    reach the identity resolver but it did not resolve to a known principal. This is
    an authentication failure (not a validation failure), because the system cannot
    attribute the request to any buyer.
    """

    @pytest.mark.asyncio
    async def test_none_principal_id_raises_authentication_error(self):
        """TC-AUTH-002: identity.principal_id=None → AdCPAuthenticationError.

        WHY THIS TEST EXISTS:
        principal_id is the key used to look up the buyer throughout the create flow
        (DB queries, audit logs, idempotency dedup). If it is None, every downstream
        lookup would either fail with a TypeError or silently match no rows. The guard
        surfaces this as AuthenticationError so the buyer sees "authentication required"
        rather than a confusing database error.
        """
        from src.core.tools.media_buy_create import _create_media_buy_impl

        identity = ResolvedIdentity(
            principal_id=None,  # the condition under test
            tenant_id="test_tenant",
            tenant={"tenant_id": "test_tenant"},
            auth_token="test-token",
            protocol="mcp",
            testing_context=AdCPTestContext(dry_run=False, test_session_id="unit-test"),
        )

        req = _minimal_valid_request()

        with pytest.raises(AdCPAuthenticationError) as exc_info:
            await _create_media_buy_impl(req=req, identity=identity)

        assert "Principal ID not found" in str(exc_info.value), (
            "Error must explain that principal_id was missing, not just that auth failed."
        )


# ===========================================================================
# TC-AUTH-003  tenant = None
# ===========================================================================


class TestTenantNoneGuard:
    """TC-AUTH-003: _create_media_buy_impl raises when the tenant context is missing.

    tenant is the dict (or TenantContext) carrying publisher-side configuration:
    currency limits, approval settings, adapter config, etc. Without it, no
    business rule can be evaluated. The guard fires before any of those lookups.
    """

    @pytest.mark.asyncio
    async def test_none_tenant_raises_authentication_error(self):
        """TC-AUTH-003: identity.tenant=None → AdCPAuthenticationError.

        WHY THIS TEST EXISTS:
        tenant is populated by resolve_identity() from the DB or ContextVar. If it
        is None, the caller reached the impl without a valid tenant — this indicates
        a misconfigured transport layer or a test that forgot to set up the tenant
        fixture. Surfacing it as AdCPAuthenticationError (not ValueError or KeyError)
        gives the transport a predictable exception type to map to a 401/403 response.
        """
        from src.core.tools.media_buy_create import _create_media_buy_impl

        identity = ResolvedIdentity(
            principal_id="principal_1",
            tenant_id="test_tenant",
            tenant=None,  # the condition under test
            auth_token="test-token",
            protocol="mcp",
            testing_context=AdCPTestContext(dry_run=False, test_session_id="unit-test"),
        )

        req = _minimal_valid_request()

        with pytest.raises(AdCPAuthenticationError) as exc_info:
            await _create_media_buy_impl(req=req, identity=identity)

        assert "tenant" in str(exc_info.value).lower(), (
            "Error must mention 'tenant' so the developer knows what is missing."
        )

    @pytest.mark.asyncio
    async def test_empty_dict_tenant_raises_authentication_error(self):
        """TC-AUTH-003b: identity.tenant={} (empty dict, falsy) → AdCPAuthenticationError.

        WHY THIS TEST EXISTS:
        The guard is `if not tenant:`. An empty dict {} evaluates as falsy in Python.
        This variant pins that the check works not only for None but for any falsy
        tenant value, preventing a future change from `if not tenant` to `if tenant is None`
        which would silently let through an empty dict and cause KeyError on tenant["tenant_id"].
        """
        from src.core.tools.media_buy_create import _create_media_buy_impl

        identity = ResolvedIdentity(
            principal_id="principal_1",
            tenant_id="test_tenant",
            tenant={},  # empty dict — also falsy
            auth_token="test-token",
            protocol="mcp",
            testing_context=AdCPTestContext(dry_run=False, test_session_id="unit-test"),
        )

        req = _minimal_valid_request()

        with pytest.raises(AdCPAuthenticationError):
            await _create_media_buy_impl(req=req, identity=identity)