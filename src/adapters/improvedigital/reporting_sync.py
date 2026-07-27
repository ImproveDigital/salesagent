"""Reporting sync for the Improve Digital adapter — Phase 3 stub.

The full implementation (see ``docs/adapters/improvedigital/INTEGRATION_PLAN.md``
Phase 3) pulls definitive delivery metrics from the Improve Marketplace
Report API — ``POST /report/ext/preview`` (synchronous JSON, ≤500 rows) with
dimensions ``campaign_id, line_item_id`` and metrics ``impressions, clicks,
advertiser_payout, complete``, falling back to the async
``POST /report/ext/generation`` job for tenants exceeding 500 rows — and
upserts per-line-item rows (spend in micros) into an
``improvedigital_line_item_stats`` cache table.

Until that lands, ``ImproveDigitalAdapter.capabilities.supports_reporting_sync``
stays ``False`` so the shared sync scheduler never invokes this path; any
direct call fails loudly below.
"""

from __future__ import annotations


class ReportingSyncNotImplemented(RuntimeError):
    """Raised when the Phase 3 reporting sync is invoked before it exists."""

    def __init__(self) -> None:
        super().__init__(
            "Improve Digital reporting sync is not implemented yet — the Report API "
            "client and line-item stats cache land in Phase 3 of "
            "docs/adapters/improvedigital/INTEGRATION_PLAN.md. Flip "
            "AdapterCapabilities.supports_reporting_sync to True only alongside "
            "that implementation."
        )


class ImproveDigitalReportingSync:
    """Placeholder — constructing it fails loudly until Phase 3 lands."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise ReportingSyncNotImplemented()
