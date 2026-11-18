#!/usr/bin/env python3
"""
Tests for error format consistency across MCP and A2A transports.

Verifies that:
1. MCP tool errors have consistent structure (ToolError with message)
2. A2A skill errors have consistent JSON-RPC error structure (ServerError)
3. The SAME error scenario produces consistent error types/messages across transports

These are unit tests that mock database/adapter calls to isolate error formatting.
"""

from unittest.mock import patch

import pytest

from src.core.exceptions import AdCPAuthenticationError, AdCPValidationError
from tests.factories.spec_required_kwargs import required_request_kwargs


class TestMCPErrorShapes:
    """Test that MCP tool errors have consistent structure."""

    @pytest.mark.asyncio
    async def test_auth_error_raises_validation_error(self):
        """MCP _create_media_buy_impl raises AdCPValidationError when identity is None."""
        from src.core.schemas import CreateMediaBuyRequest
        from src.core.tools.media_buy_create import _create_media_buy_impl

        # Build a minimal valid request
        req = CreateMediaBuyRequest(
            **required_request_kwargs(),
            brand={"domain": "testbrand.com"},
            packages=[],
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-02-01T00:00:00Z",
        )

        # _create_media_buy_impl requires identity; passing None triggers AdCPValidationError
        with pytest.raises(AdCPValidationError) as exc_info:
            await _create_media_buy_impl(req=req, identity=None)

        assert "Identity is required" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_not_found_principal_returns_error_response(self):
        """MCP _create_media_buy_impl returns error response for non-existent principal."""
        from src.core.resolved_identity import ResolvedIdentity
        from src.core.schemas import CreateMediaBuyRequest
        from src.core.testing_hooks import AdCPTestContext
        from src.core.tools.media_buy_create import _create_media_buy_impl

        req = CreateMediaBuyRequest(
            **required_request_kwargs(),
            brand={"domain": "testbrand.com"},
            packages=[],
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-02-01T00:00:00Z",
        )

        identity = ResolvedIdentity(
            principal_id="nonexistent",
            tenant_id="test",
            tenant={"tenant_id": "test"},
            testing_context=AdCPTestContext(dry_run=False, test_session_id="test"),
        )

        with (
            patch("src.core.helpers.context_helpers.ensure_tenant_context", return_value={"tenant_id": "test"}),
            patch("src.core.tools.media_buy_create.validate_setup_complete"),
            patch("src.core.tools.media_buy_create.get_principal_object", return_value=None),
        ):
            result = await _create_media_buy_impl(req=req, identity=identity)

        # Should return a CreateMediaBuyResult with error response
        assert hasattr(result, "response")
        response = result.response
        assert hasattr(response, "errors")
        assert response.errors is not None
        assert len(response.errors) > 0
        assert response.errors[0].code == "authentication_error"


class TestUpdateMediaBuyErrorShapes:
    """Test that update_media_buy error paths produce consistent errors."""

    @pytest.mark.asyncio
    async def test_missing_context_raises_value_error(self):
        """update_media_buy _impl raises ValueError when identity is None."""
        from src.core.schemas import UpdateMediaBuyRequest
        from src.core.tools.media_buy_update import _update_media_buy_impl

        req = UpdateMediaBuyRequest(
            **required_request_kwargs(),
            media_buy_id="buy_001",
        )

        with pytest.raises(ValueError) as exc_info:
            _update_media_buy_impl(req=req, identity=None)

        assert "Identity is required" in str(exc_info.value)


class TestListCreativesErrorShapes:
    """Test that list_creatives error paths produce consistent errors."""

    @pytest.mark.asyncio
    async def test_missing_auth_raises_authentication_error(self):
        """list_creatives _impl raises AdCPAuthenticationError when identity is None."""
        from src.core.tools.creatives.listing import _list_creatives_impl

        with pytest.raises(AdCPAuthenticationError) as exc_info:
            _list_creatives_impl(identity=None)

        assert "x-adcp-auth" in str(exc_info.value).lower() or "Missing" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Recovery field in MCP error responses
# ---------------------------------------------------------------------------


class TestMCPRecoveryInErrorResponses:
    """Verify that MCP ToolError carries recovery for every AdCPError subclass.

    The MCP boundary (with_error_logging) translates AdCPError -> ToolError(code, msg, recovery).
    Buyer agents parse ToolError.args to decide retry/fix/abandon strategy.
    """

    @pytest.mark.parametrize(
        "exc_class,msg,expected_code,expected_recovery",
        [
            ("AdCPError", "internal error", "INTERNAL_ERROR", "terminal"),
            ("AdCPValidationError", "bad field", "VALIDATION_ERROR", "correctable"),
            ("AdCPAuthenticationError", "bad token", "AUTH_TOKEN_INVALID", "terminal"),
            ("AdCPAuthorizationError", "no access", "AUTHORIZATION_ERROR", "terminal"),
            ("AdCPNotFoundError", "gone", "NOT_FOUND", "correctable"),
            ("AdCPConflictError", "duplicate", "CONFLICT", "correctable"),
            ("AdCPGoneError", "expired", "GONE", "terminal"),
            ("AdCPBudgetExhaustedError", "no budget", "BUDGET_EXHAUSTED", "correctable"),
            ("AdCPRateLimitError", "slow down", "RATE_LIMIT_EXCEEDED", "transient"),
            ("AdCPAdapterError", "GAM down", "ADAPTER_ERROR", "transient"),
            ("AdCPServiceUnavailableError", "offline", "SERVICE_UNAVAILABLE", "transient"),
        ],
        ids=lambda x: x if isinstance(x, str) and x.startswith("AdCP") else "",
    )
    def test_mcp_tool_error_carries_recovery(self, exc_class, msg, expected_code, expected_recovery):
        """ToolError from MCP boundary carries recovery in args[2] for {exc_class}."""
        from fastmcp.exceptions import ToolError

        import src.core.exceptions as exc_mod
        from src.core.tool_error_logging import with_error_logging

        klass = getattr(exc_mod, exc_class)

        def failing_tool():
            raise klass(msg)

        wrapped = with_error_logging(failing_tool)

        with pytest.raises(ToolError) as exc_info:
            wrapped()

        tool_error = exc_info.value
        assert tool_error.args[0] == expected_code
        assert tool_error.args[1] == msg
        assert len(tool_error.args) >= 3, f"ToolError for {exc_class} must have 3 args (code, msg, recovery)"
        assert tool_error.args[2] == expected_recovery


# ---------------------------------------------------------------------------
# Recovery override preservation through serialization
# ---------------------------------------------------------------------------


class TestRecoveryOverrideInSerialization:
    """Verify custom recovery= override is preserved through all serialization paths."""

    def test_custom_recovery_override_in_to_dict(self):
        """to_dict() reflects custom recovery, not class default."""
        from src.core.exceptions import AdCPConflictError

        # Default recovery is "correctable"
        default = AdCPConflictError("dup")
        assert default.to_dict()["recovery"] == "correctable"

        # Override to "terminal" (e.g., non-retryable conflict)
        overridden = AdCPConflictError("permanent conflict", recovery="terminal")
        assert overridden.to_dict()["recovery"] == "terminal"

    def test_custom_recovery_survives_mcp_then_extract(self):
        """Custom recovery: AdCPError(recovery=X) -> ToolError -> extract_error_info -> X."""
        from fastmcp.exceptions import ToolError

        from src.core.exceptions import AdCPAdapterError
        from src.core.tool_error_logging import extract_error_info, with_error_logging

        def failing():
            raise AdCPAdapterError("permanent failure", recovery="terminal")

        wrapped = with_error_logging(failing)

        with pytest.raises(ToolError) as exc_info:
            wrapped()

        code, message, recovery = extract_error_info(exc_info.value)
        assert recovery == "terminal"  # Custom, not default "transient"


# ---------------------------------------------------------------------------
# Vocabulary consistency: error_codes match adcp-req canonical vocabulary
# ---------------------------------------------------------------------------


class TestErrorCodeVocabularyConsistency:
    """Validate error_code strings against adcp-req ERROR_CODE_VOCABULARY.md.

    The canonical vocabulary is defined in:
    /docs/requirements/ERROR_CODE_VOCABULARY.md (adcp-req repo)

    Our exception hierarchy must use canonical codes where the spec defines them.
    Salesagent-specific codes (INTERNAL_ERROR, AUTH_TOKEN_INVALID, etc.) are
    allowed as vocabulary extensions but must be explicitly declared.
    """

    # Canonical codes from adcp-req spec + salesagent extensions
    CANONICAL_ERROR_CODES = {
        "INTERNAL_ERROR",  # HTTP 500 catch-all (salesagent extension)
        "VALIDATION_ERROR",  # adcp-req: Generic Errors
        "AUTH_TOKEN_INVALID",  # HTTP 401 (salesagent extension)
        "AUTHORIZATION_ERROR",  # HTTP 403 (salesagent extension)
        "INVALID_REQUEST",  # HTTP 400 — spec-level invalid request payload
        "NOT_FOUND",  # Generic form of {ENTITY}_NOT_FOUND
        "MEDIA_BUY_NOT_FOUND",  # HTTP 404 — media buy not found within caller's tenant
        "PACKAGE_NOT_FOUND",  # HTTP 404 — package not found on the referenced media buy
        "PRODUCT_NOT_FOUND",  # HTTP 404 — referenced product_id unknown or expired
        "ACCOUNT_NOT_FOUND",  # adcp-req: Account resolution (BR-RULE-080)
        "ACCOUNT_AMBIGUOUS",  # adcp-req: Natural key matches multiple accounts (BR-RULE-080)
        "ACCOUNT_SETUP_REQUIRED",  # adcp-req: Account requires setup (BR-RULE-080)
        "ACCOUNT_SUSPENDED",  # adcp-req: Account is suspended (BR-RULE-080)
        "ACCOUNT_PAYMENT_REQUIRED",  # adcp-req: Account has outstanding payment (BR-RULE-080)
        "CONFLICT",  # Generic form of {ENTITY}_EXISTS
        "GONE",  # HTTP 410 (salesagent extension)
        "NOT_CANCELLABLE",  # HTTP 422 — media buy already in a terminal state
        "INVALID_STATE",  # HTTP 422 — action not legal for the entity's current state
        "BUDGET_EXHAUSTED",  # HTTP 422 (salesagent extension)
        "TERMS_REJECTED",  # HTTP 422 — measurement_terms/performance_standards cannot be honored
        "UNSUPPORTED_FEATURE",  # HTTP 422 — request relies on an undeclared capability
        "RATE_LIMIT_EXCEEDED",  # adcp-req: Rate Limiting / Quota Errors
        "ADAPTER_ERROR",  # HTTP 502 (salesagent extension)
        "CONFIGURATION_ERROR",  # HTTP 500 — decryption/config broken (salesagent extension)
        "SERVICE_UNAVAILABLE",  # adcp-req: Service/Infrastructure Errors
        "NOT_IMPLEMENTED_IN_EMBEDDED",  # HTTP 501 — workflow owned by the embedding storefront
    }

    def test_all_exception_error_codes_are_canonical(self):
        """Every AdCPError subclass error_code must be in the canonical vocabulary."""
        from src.core.exceptions import (
            AdCPAdapterError,
            AdCPAuthenticationError,
            AdCPAuthorizationError,
            AdCPBudgetExhaustedError,
            AdCPConflictError,
            AdCPError,
            AdCPGoneError,
            AdCPNotFoundError,
            AdCPRateLimitError,
            AdCPServiceUnavailableError,
            AdCPValidationError,
        )

        exception_classes = [
            AdCPError,
            AdCPValidationError,
            AdCPAuthenticationError,
            AdCPAuthorizationError,
            AdCPNotFoundError,
            AdCPConflictError,
            AdCPGoneError,
            AdCPBudgetExhaustedError,
            AdCPRateLimitError,
            AdCPAdapterError,
            AdCPServiceUnavailableError,
        ]

        for exc_class in exception_classes:
            code = exc_class.error_code
            assert code in self.CANONICAL_ERROR_CODES, (
                f"{exc_class.__name__}.error_code = {code!r} is not in the canonical vocabulary. "
                f"If this is a new code, add it to CANONICAL_ERROR_CODES with a comment. "
                f"If this is a renamed code, update the exception class."
            )

    def test_rate_limit_uses_canonical_code(self):
        """AdCPRateLimitError must use RATE_LIMIT_EXCEEDED (not RATE_LIMITED).

        adcp-req ERROR_CODE_VOCABULARY.md defines RATE_LIMIT_EXCEEDED as canonical.
        RATE_LIMITED and THROTTLED are anti-patterns.
        """
        from src.core.exceptions import AdCPRateLimitError

        assert AdCPRateLimitError.error_code == "RATE_LIMIT_EXCEEDED", (
            f"AdCPRateLimitError.error_code = {AdCPRateLimitError.error_code!r}, "
            f"expected 'RATE_LIMIT_EXCEEDED' per adcp-req vocabulary"
        )

    def test_canonical_vocabulary_covers_all_subclasses(self):
        """CANONICAL_ERROR_CODES must have exactly one entry per exception subclass."""
        from src.core.exceptions import AdCPError

        # Discover all concrete subclasses (recursively)
        subclass_codes = set()

        def _collect(cls: type) -> None:
            subclass_codes.add(cls.error_code)
            for sub in cls.__subclasses__():
                _collect(sub)

        _collect(AdCPError)

        # Every subclass code must be in canonical set
        missing = subclass_codes - self.CANONICAL_ERROR_CODES
        assert not missing, (
            f"Exception error_codes not in CANONICAL_ERROR_CODES: {missing}. "
            f"Add them to the canonical set or fix the error_code."
        )

        # Every canonical code must correspond to a subclass
        unused = self.CANONICAL_ERROR_CODES - subclass_codes
        assert not unused, (
            f"CANONICAL_ERROR_CODES entries without a matching exception: {unused}. "
            f"Remove stale entries or create the missing exception class."
        )
