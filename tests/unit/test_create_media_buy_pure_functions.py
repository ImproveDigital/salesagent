"""Unit tests for create_media_buy pure helper functions.

No database, no adapter, no HTTP — these functions are self-contained.
They encode business rules, so any refactor that silently changes their
behaviour will be caught immediately by `make quality`.

Covered gaps (not in any existing test file as of this writing):
  TC-STAT-006 — _determine_media_buy_status: terminal state wins over creative state
  TC-MEAS-002 — _validate_measurement_terms: just below the 5% floor is rejected
  TC-MEAS-003 — _validate_measurement_terms: exactly 5.0% (boundary) is accepted
  TC-MEAS-006 — _validate_measurement_terms: billing_measurement=None skips the check
  TC-CASN-001 — _get_requested_creative_assignments: creative_ids shorthand → weight=100
  TC-CASN-002 — _get_requested_creative_assignments: explicit weight preserved
  TC-CASN-003 — _get_requested_creative_assignments: placement_ids preserved
  TC-CASN-004 — _get_requested_creative_assignments: assignments wins on duplicate ID
  TC-CASN-005 — _get_requested_creative_assignments: empty creative_ids returns []
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from src.core.exceptions import AdCPTermsRejectedError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_package_with_variance(variance: float | None) -> MagicMock:
    """Build a mock PackageRequest with measurement_terms.billing_measurement.max_variance_percent."""
    billing = MagicMock()
    billing.max_variance_percent = variance

    terms = MagicMock()
    terms.billing_measurement = billing

    pkg = MagicMock()
    pkg.measurement_terms = terms
    return pkg


# ===========================================================================
# _determine_media_buy_status
# ===========================================================================


class TestMediaBuyStatusDetermination:
    """Tests for _determine_media_buy_status.

    Priority order (highest wins):
    1. completed  — now > end_time (terminal; always checked first)
    2. pending_creatives — no creatives assigned
    3. pending_start — manual approval OR future start
    4. active — currently delivering (has creatives, in-flight, approved)

    The existing tests in test_media_buy.py cover UC-002-ST01..06.
    This class fills TC-STAT-006: the terminal 'completed' state must win
    even when no creatives are assigned (i.e. priority-1 beats priority-2).
    """

    def test_no_creatives_but_past_end_date_yields_completed(self):
        """TC-STAT-006: terminal state (completed) beats creative state (pending_creatives).

        WHY THIS TEST EXISTS:
        The status function checks 'completed' FIRST because a buy that is over
        is over — it cannot be 'pending_creatives' if the flight window has closed.
        Without this guard the function could return 'pending_creatives' for expired
        buys that never received creatives, misleading the buyer into thinking they
        still need to act.

        Regression guard for any future refactor that changes the priority order
        from [completed, pending_creatives, pending_start, active] to something else.
        """
        from src.core.tools.media_buy_create import _determine_media_buy_status

        now = datetime(2026, 5, 1, tzinfo=UTC)
        start = datetime(2026, 3, 1, tzinfo=UTC)
        end = datetime(2026, 4, 30, tzinfo=UTC)  # end is before now

        # has_creatives=False would normally yield pending_creatives,
        # but end_time is in the past so completed must win.
        result = _determine_media_buy_status(
            manual_approval_required=False,
            has_creatives=False,
            start_time=start,
            end_time=end,
            now=now,
        )

        assert result == "completed", (
            "A buy whose end_time has passed must be 'completed' even if no creatives "
            "were ever assigned. 'completed' is terminal and is checked before 'pending_creatives'."
        )


# ===========================================================================
# _validate_measurement_terms
# ===========================================================================


class TestMeasurementTermsValidation:
    """Tests for _validate_measurement_terms.

    The seller enforces a floor of 5.0% variance (MIN_SUPPORTED_VARIANCE_PERCENT).
    Anything tighter is physically impossible across independent counting methods
    (DV360, IAS, MOAT). The error is 'correctable': the buyer must relax the
    variance and retry with a fresh idempotency_key.

    The existing behavioral tests cover:
      - variance=0 (far below floor) → rejected  (test_aggressive_max_variance_percent…)
      - variance=10 (well above floor) → accepted (test_relaxed_max_variance_percent…)
      - no measurement_terms at all → accepted    (test_no_measurement_terms…)

    This class adds the boundary conditions:
      TC-MEAS-002: 4.9 — just below floor → rejected
      TC-MEAS-003: 5.0 — exactly at floor → accepted (< not <=)
      TC-MEAS-006: billing_measurement=None → check skipped
    """

    def test_variance_just_below_floor_raises_terms_rejected(self):
        """TC-MEAS-002: 4.9% is below the 5% floor and must be rejected.

        WHY THIS TEST EXISTS:
        The floor is checked with a strict less-than (`float(variance) < 5.0`).
        This test confirms that even 0.1% below the floor is rejected, ensuring
        the boundary is enforced consistently. A rounding bug (e.g. using <=)
        would wrongly accept 4.9.
        """
        from src.core.tools.media_buy_create import _validate_measurement_terms

        req = MagicMock()
        req.packages = [_make_package_with_variance(4.9)]

        with pytest.raises(AdCPTermsRejectedError) as exc_info:
            _validate_measurement_terms(req)

        assert "4.9" in str(exc_info.value), "Error should name the offending variance value."
        assert exc_info.value.recovery == "correctable", (
            "Per AdCP spec, TERMS_REJECTED must be correctable so the buyer can relax and retry."
        )

    def test_variance_exactly_at_floor_passes_validation(self):
        """TC-MEAS-003: 5.0% is the minimum acceptable variance and must NOT be rejected.

        WHY THIS TEST EXISTS:
        The check uses strict less-than (`< 5.0`), not less-than-or-equal (`<= 5.0`).
        Passing 5.0 must succeed — it is the documented floor. If someone changes
        the operator to `<=`, this test catches it immediately.
        """
        from src.core.tools.media_buy_create import _validate_measurement_terms

        req = MagicMock()
        req.packages = [_make_package_with_variance(5.0)]

        # Should not raise — exactly at the floor is acceptable.
        _validate_measurement_terms(req)

    def test_billing_measurement_none_skips_variance_check(self):
        """TC-MEAS-006: billing_measurement=None means no measurement contract was proposed.

        WHY THIS TEST EXISTS:
        The seller only rejects when the buyer ACTIVELY proposes terms that are too
        tight. If billing_measurement is None (absent), there are no terms to reject.
        The function uses `getattr(terms, "billing_measurement", None)` defensively;
        this test pins that the early-return branch is taken when billing_measurement
        is None, so a NoneType attribute access never raises.
        """
        from src.core.tools.media_buy_create import _validate_measurement_terms

        terms = MagicMock()
        terms.billing_measurement = None  # absent billing contract

        pkg = MagicMock()
        pkg.measurement_terms = terms

        req = MagicMock()
        req.packages = [pkg]

        # Should not raise — absence of billing_measurement means no terms to validate.
        _validate_measurement_terms(req)


# ===========================================================================
# _get_requested_creative_assignments
# ===========================================================================


class TestCreativeAssignmentNormalization:
    """Tests for _get_requested_creative_assignments.

    Two input shapes map to the same normalised output
    [{creative_id, weight, placement_ids}]:

      1. creative_ids: ["id1", "id2"]  — legacy shorthand (local extension)
      2. creative_assignments: [{creative_id, weight, placement_ids}] — AdCP spec shape

    Both shapes must survive normalisation with correct defaults so the downstream
    adapter and status-determination logic never sees raw lists of string IDs.
    """

    def _package_with_ids(self, creative_ids: list[str] | None, assignments=None) -> MagicMock:
        """Minimal package mock with configurable creative_ids and creative_assignments."""
        pkg = MagicMock()
        pkg.creative_ids = creative_ids
        pkg.creative_assignments = assignments or []
        return pkg

    def test_creative_ids_shorthand_defaults_to_weight_100_and_no_placements(self):
        """TC-CASN-001: creative_ids list is normalised to weight=100, placement_ids=None.

        WHY THIS TEST EXISTS:
        creative_ids is the legacy shorthand. Adapters and the status function
        consume the normalised form {creative_id, weight, placement_ids}. If
        shorthand IDs are left as raw strings, adapters that use weight for
        rotation scheduling will silently treat all creatives as weight-0.
        Default weight=100 means 'equal rotation among all assigned creatives'.
        """
        from src.core.tools.media_buy_create import _get_requested_creative_assignments

        pkg = self._package_with_ids(["creative_a", "creative_b"])
        result = _get_requested_creative_assignments(pkg)

        assert len(result) == 2
        for entry in result:
            assert entry["weight"] == 100, "Shorthand IDs must default to weight=100."
            assert entry["placement_ids"] is None, "Shorthand IDs must default to placement_ids=None."
        assert {e["creative_id"] for e in result} == {"creative_a", "creative_b"}

    def test_creative_assignments_explicit_weight_preserved(self):
        """TC-CASN-002: creative_assignments with an explicit weight are kept as-is.

        WHY THIS TEST EXISTS:
        Buyers use creative_assignments to control impression-weighted rotation
        (e.g. 70/30 split). If the normaliser silently resets the weight to 100,
        the split is lost and both creatives serve equally. This pins the contract
        that whatever the buyer sends is what the adapter receives.
        """
        from src.core.tools.media_buy_create import _get_requested_creative_assignments

        assignment = MagicMock()
        assignment.creative_id = "creative_x"
        assignment.weight = 70
        assignment.placement_ids = None

        pkg = self._package_with_ids(None, [assignment])
        result = _get_requested_creative_assignments(pkg)

        assert len(result) == 1
        assert result[0]["creative_id"] == "creative_x"
        assert result[0]["weight"] == 70, "Explicit weight must be preserved, not overwritten with 100."

    def test_creative_assignments_placement_ids_preserved(self):
        """TC-CASN-003: placement_ids survive normalisation intact.

        WHY THIS TEST EXISTS:
        placement_ids restrict which placements a creative may serve on (e.g.
        mobile-only or header-bid-only). Dropping them in normalisation would
        cause the creative to serve everywhere, violating the buyer's targeting
        intent. This test ensures the list is passed through unchanged.
        """
        from src.core.tools.media_buy_create import _get_requested_creative_assignments

        assignment = MagicMock()
        assignment.creative_id = "creative_y"
        assignment.weight = 100
        assignment.placement_ids = ["placement_1", "placement_2"]

        pkg = self._package_with_ids(None, [assignment])
        result = _get_requested_creative_assignments(pkg)

        assert result[0]["placement_ids"] == ["placement_1", "placement_2"], (
            "placement_ids must survive normalisation unchanged."
        )

    def test_same_id_in_both_creative_ids_and_assignments_uses_assignment_entry(self):
        """TC-CASN-004: when a creative_id appears in both creative_ids (shorthand) and
        creative_assignments (full spec), the creative_assignments entry wins.

        WHY THIS TEST EXISTS:
        A buyer may send creative_ids for backward compatibility while also sending
        creative_assignments to override the default weight. Without this precedence
        rule, the shorthand's weight=100 default would silently override the explicit
        weight from creative_assignments, breaking impression-weighting contracts.
        The deduplication is by ID (dict keyed on creative_id), so the last write wins
        — assignments are processed after ids, so assignments always win.
        """
        from src.core.tools.media_buy_create import _get_requested_creative_assignments

        # creative_ids sets weight=100 by default; assignment overrides to 30.
        assignment = MagicMock()
        assignment.creative_id = "creative_z"
        assignment.weight = 30
        assignment.placement_ids = None

        pkg = self._package_with_ids(["creative_z"], [assignment])
        result = _get_requested_creative_assignments(pkg)

        assert len(result) == 1, "Duplicate ID must be deduplicated to a single entry."
        assert result[0]["weight"] == 30, (
            "creative_assignments entry (weight=30) must win over the creative_ids default (weight=100)."
        )

    def test_empty_creative_ids_list_returns_empty_list(self):
        """TC-CASN-005: creative_ids=[] (empty list) is treated as no creative assignment.

        WHY THIS TEST EXISTS:
        Buyers may send creative_ids=[] explicitly (e.g. when clearing a previous
        assignment in a request template). The normaliser must return [] (not None,
        not raising) so callers can rely on `bool(assignments) == False` to detect
        'no creatives'. If the function returned None, callers without a None-guard
        would crash on `len(result)`.
        """
        from src.core.tools.media_buy_create import _get_requested_creative_assignments

        pkg = self._package_with_ids([])
        result = _get_requested_creative_assignments(pkg)

        assert result == [], "empty creative_ids must normalise to an empty list, not None."
        assert isinstance(result, list), "Return type must always be list, never None."
