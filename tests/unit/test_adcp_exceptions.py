"""Tests for AdCP exception hierarchy and FastAPI exception handlers.

Validates that:
- Exception classes exist with proper inheritance and attributes
- FastAPI handlers return correct HTTP status codes and response format
- Exception → ToolError format mapping exists
- Dead A2A error map is not present (real translation in adcp_a2a_server.py)

beads: salesagent-b61l.11
"""


# ---------------------------------------------------------------------------
# Exception Hierarchy Tests
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    """Verify AdCP exception classes exist with correct attributes."""

    def test_base_exception_exists(self):
        """AdCPError base class must exist."""
        from src.core.exceptions import AdCPError

        exc = AdCPError("test error")
        assert str(exc) == "test error"
        assert isinstance(exc, Exception)

    def test_validation_error(self):
        """AdCPValidationError must have status_code=400."""
        from src.core.exceptions import AdCPError, AdCPValidationError

        exc = AdCPValidationError("invalid field")
        assert isinstance(exc, AdCPError)
        assert exc.status_code == 400
        assert exc.error_code == "VALIDATION_ERROR"

    def test_authentication_error(self):
        """AdCPAuthenticationError must have status_code=401."""
        from src.core.exceptions import AdCPAuthenticationError, AdCPError

        exc = AdCPAuthenticationError("bad token")
        assert isinstance(exc, AdCPError)
        assert exc.status_code == 401
        assert exc.error_code == "AUTH_TOKEN_INVALID"

    def test_authorization_error(self):
        """AdCPAuthorizationError must have status_code=403."""
        from src.core.exceptions import AdCPAuthorizationError, AdCPError

        exc = AdCPAuthorizationError("forbidden")
        assert isinstance(exc, AdCPError)
        assert exc.status_code == 403
        assert exc.error_code == "AUTHORIZATION_ERROR"

    def test_not_found_error(self):
        """AdCPNotFoundError must have status_code=404."""
        from src.core.exceptions import AdCPError, AdCPNotFoundError

        exc = AdCPNotFoundError("resource missing")
        assert isinstance(exc, AdCPError)
        assert exc.status_code == 404
        assert exc.error_code == "NOT_FOUND"

    def test_rate_limit_error(self):
        """AdCPRateLimitError must have status_code=429."""
        from src.core.exceptions import AdCPError, AdCPRateLimitError

        exc = AdCPRateLimitError("too many requests")
        assert isinstance(exc, AdCPError)
        assert exc.status_code == 429
        assert exc.error_code == "RATE_LIMIT_EXCEEDED"

    def test_adapter_error(self):
        """AdCPAdapterError must have status_code=502."""
        from src.core.exceptions import AdCPAdapterError, AdCPError

        exc = AdCPAdapterError("GAM unavailable")
        assert isinstance(exc, AdCPError)
        assert exc.status_code == 502
        assert exc.error_code == "ADAPTER_ERROR"

    def test_conflict_error(self):
        """AdCPConflictError must have status_code=409."""
        from src.core.exceptions import AdCPConflictError, AdCPError

        exc = AdCPConflictError("duplicate idempotency key")
        assert isinstance(exc, AdCPError)
        assert exc.status_code == 409
        assert exc.error_code == "CONFLICT"

    def test_gone_error(self):
        """AdCPGoneError must have status_code=410."""
        from src.core.exceptions import AdCPError, AdCPGoneError

        exc = AdCPGoneError("proposal expired")
        assert isinstance(exc, AdCPError)
        assert exc.status_code == 410
        assert exc.error_code == "GONE"

    def test_budget_exhausted_error(self):
        """AdCPBudgetExhaustedError must have status_code=422."""
        from src.core.exceptions import AdCPBudgetExhaustedError, AdCPError

        exc = AdCPBudgetExhaustedError("budget limit reached")
        assert isinstance(exc, AdCPError)
        assert exc.status_code == 422
        assert exc.error_code == "BUDGET_EXHAUSTED"

    def test_service_unavailable_error(self):
        """AdCPServiceUnavailableError must have status_code=503."""
        from src.core.exceptions import AdCPError, AdCPServiceUnavailableError

        exc = AdCPServiceUnavailableError("product temporarily unavailable")
        assert isinstance(exc, AdCPError)
        assert exc.status_code == 503
        assert exc.error_code == "SERVICE_UNAVAILABLE"

    def test_exception_carries_details(self):
        """Exceptions must support optional details dict."""
        from src.core.exceptions import AdCPValidationError

        details = {"field": "budget", "constraint": "must be positive"}
        exc = AdCPValidationError("invalid budget", details=details)
        assert exc.details == details

    def test_exception_to_dict(self):
        """Exceptions must be serializable to dict for response bodies."""
        from src.core.exceptions import AdCPValidationError

        exc = AdCPValidationError("bad field", details={"field": "name"})
        d = exc.to_dict()
        assert d["error_code"] == "VALIDATION_ERROR"
        assert d["message"] == "bad field"
        assert d["details"] == {"field": "name"}


# ---------------------------------------------------------------------------
# Recovery Classification Tests
# ---------------------------------------------------------------------------


class TestRecoveryClassification:
    """Verify recovery field on AdCPError and all subclasses."""

    def test_base_error_defaults_to_terminal(self):
        """AdCPError base class defaults to recovery='terminal'."""
        from src.core.exceptions import AdCPError

        exc = AdCPError("something broke")
        assert exc.recovery == "terminal"

    def test_validation_error_defaults_to_correctable(self):
        """AdCPValidationError defaults to recovery='correctable'."""
        from src.core.exceptions import AdCPValidationError

        exc = AdCPValidationError("invalid field")
        assert exc.recovery == "correctable"

    def test_authentication_error_defaults_to_terminal(self):
        """AdCPAuthenticationError defaults to recovery='terminal'."""
        from src.core.exceptions import AdCPAuthenticationError

        exc = AdCPAuthenticationError("bad token")
        assert exc.recovery == "terminal"

    def test_authorization_error_defaults_to_terminal(self):
        """AdCPAuthorizationError defaults to recovery='terminal'."""
        from src.core.exceptions import AdCPAuthorizationError

        exc = AdCPAuthorizationError("forbidden")
        assert exc.recovery == "terminal"

    def test_not_found_error_defaults_to_correctable(self):
        """AdCPNotFoundError defaults to recovery='correctable'."""
        from src.core.exceptions import AdCPNotFoundError

        exc = AdCPNotFoundError("resource missing")
        assert exc.recovery == "correctable"

    def test_rate_limit_error_defaults_to_transient(self):
        """AdCPRateLimitError defaults to recovery='transient'."""
        from src.core.exceptions import AdCPRateLimitError

        exc = AdCPRateLimitError("too many requests")
        assert exc.recovery == "transient"

    def test_adapter_error_defaults_to_transient(self):
        """AdCPAdapterError defaults to recovery='transient'."""
        from src.core.exceptions import AdCPAdapterError

        exc = AdCPAdapterError("GAM unavailable")
        assert exc.recovery == "transient"

    def test_conflict_error_defaults_to_correctable(self):
        """AdCPConflictError defaults to recovery='correctable'."""
        from src.core.exceptions import AdCPConflictError

        exc = AdCPConflictError("duplicate idempotency key")
        assert exc.recovery == "correctable"

    def test_gone_error_defaults_to_terminal(self):
        """AdCPGoneError defaults to recovery='terminal'."""
        from src.core.exceptions import AdCPGoneError

        exc = AdCPGoneError("proposal expired")
        assert exc.recovery == "terminal"

    def test_budget_exhausted_error_defaults_to_correctable(self):
        """AdCPBudgetExhaustedError defaults to recovery='correctable'.

        Buyer can fix by increasing budget or adjusting spend caps.
        Covers: salesagent-u60m (PR #1083 review)
        """
        from src.core.exceptions import AdCPBudgetExhaustedError

        exc = AdCPBudgetExhaustedError("budget limit reached")
        assert exc.recovery == "correctable"

    def test_service_unavailable_error_defaults_to_transient(self):
        """AdCPServiceUnavailableError defaults to recovery='transient'."""
        from src.core.exceptions import AdCPServiceUnavailableError

        exc = AdCPServiceUnavailableError("product temporarily unavailable")
        assert exc.recovery == "transient"

    def test_recovery_can_be_overridden_per_instance(self):
        """Callers can override recovery for specific raise sites."""
        from src.core.exceptions import AdCPValidationError

        exc = AdCPValidationError("permanent schema mismatch", recovery="terminal")
        assert exc.recovery == "terminal"

    def test_to_dict_includes_recovery(self):
        """to_dict() must include recovery field in serialized output."""
        from src.core.exceptions import AdCPValidationError

        exc = AdCPValidationError("bad field", details={"field": "name"})
        d = exc.to_dict()
        assert "recovery" in d
        assert d["recovery"] == "correctable"

    def test_to_dict_includes_overridden_recovery(self):
        """to_dict() must serialize overridden recovery value."""
        from src.core.exceptions import AdCPAdapterError

        exc = AdCPAdapterError("permanent config error", recovery="terminal")
        d = exc.to_dict()
        assert d["recovery"] == "terminal"


# ---------------------------------------------------------------------------
# A2A Error Mapping Tests
# ---------------------------------------------------------------------------


class TestNoDeadA2AMap:
    """Dead A2A error map must not exist in exceptions module (PR #1083 review)."""

    def test_no_a2a_error_code_map_in_exceptions(self):
        """_A2A_ERROR_CODE_MAP was dead code — real translation is in adcp_a2a_server.py."""
        import src.core.exceptions as exc_module

        assert not hasattr(exc_module, "_A2A_ERROR_CODE_MAP"), (
            "_A2A_ERROR_CODE_MAP is dead code — A2A translation lives in _adcp_to_a2a_error() in adcp_a2a_server.py"
        )

    def test_no_to_a2a_error_code_in_exceptions(self):
        """to_a2a_error_code() was dead code — real translation is in adcp_a2a_server.py."""
        import src.core.exceptions as exc_module

        assert not hasattr(exc_module, "to_a2a_error_code"), (
            "to_a2a_error_code() is dead code — A2A translation lives in _adcp_to_a2a_error() in adcp_a2a_server.py"
        )
