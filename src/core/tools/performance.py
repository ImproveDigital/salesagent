"""AdCP tool implementation.

This module contains tool implementations following the MCP/A2A shared
implementation pattern from CLAUDE.md.
"""

import logging
from datetime import datetime
from typing import Any

from adcp.types import ContextObject
from pydantic import ValidationError

from src.core.exceptions import (
    AdCPAdapterError,
    AdCPAuthenticationError,
    AdCPInvalidStateError,
    AdCPNotFoundError,
    AdCPPackageNotFoundError,
    AdCPValidationError,
)

logger = logging.getLogger(__name__)

from src.core.audit_logger import get_audit_logger
from src.core.auth import get_principal_object
from src.core.database.repositories import MediaBuyUoW
from src.core.helpers.adapter_helpers import get_adapter
from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import PackagePerformance, UpdatePerformanceIndexRequest, UpdatePerformanceIndexResponse
from src.core.tools._gam_projection import not_materialized_message, unmaterialized_projected_ids
from src.core.tools.media_buy_update import _verify_principal
from src.core.tracing import traced
from src.core.validation_helpers import format_validation_error


@traced
def _update_performance_index_impl(
    media_buy_id: str,
    performance_data: list[dict[str, Any]],
    context: ContextObject | None = None,
    identity: ResolvedIdentity | None = None,
) -> UpdatePerformanceIndexResponse:
    """Shared implementation for update_performance_index (used by both MCP and A2A).

    Args:
        media_buy_id: ID of the media buy to update
        performance_data: List of performance data objects
        context: Application level context per adcp spec
        identity: Resolved identity for authentication

    Returns:
        UpdatePerformanceIndexResponse with update status
    """
    # Create request object from individual parameters (MCP-compliant)
    # Convert dict performance_data to ProductPerformance objects
    from src.core.schemas import ProductPerformance

    try:
        performance_objects = [ProductPerformance(**perf) for perf in performance_data]
        req = UpdatePerformanceIndexRequest(
            media_buy_id=media_buy_id, performance_data=performance_objects, context=context
        )
    except ValidationError as e:
        raise AdCPValidationError(format_validation_error(e, context="update_performance_index request")) from e

    if identity is None:
        raise ValueError("Identity is required for update_performance_index")

    # Tenant is resolved at the transport boundary (resolve_identity_from_context)
    tenant = identity.tenant
    if not tenant:
        raise AdCPAuthenticationError("No tenant context available")

    with MediaBuyUoW(tenant["tenant_id"]) as uow:
        assert uow.media_buys is not None
        _verify_principal(req.media_buy_id, identity, uow.media_buys)
    principal_id = identity.principal_id
    if principal_id is None:
        raise AdCPAuthenticationError("Principal ID not found in identity - authentication required")

    # Get the Principal object
    principal = get_principal_object(principal_id, tenant_id=identity.tenant_id)
    if not principal:
        raise AdCPNotFoundError(f"Principal {principal_id} not found")

    # Get the appropriate adapter (no dry_run support for performance updates)
    adapter = get_adapter(principal, dry_run=False, tenant=tenant)

    # Convert ProductPerformance to PackagePerformance for the adapter
    package_performance = [
        PackagePerformance(package_id=perf.product_id, performance_index=perf.performance_index)
        for perf in req.performance_data
    ]

    # Call the adapter's update method
    success = adapter.update_media_buy_performance_index(req.media_buy_id, package_performance)

    # Log the performance update
    logger.info("Performance Index Update for %s", req.media_buy_id)
    for perf in req.performance_data:
        logger.info(
            "  %s: %.2f (confidence: %s)",
            perf.product_id,
            perf.performance_index,
            perf.confidence_score or "N/A",
        )

    if any(p.performance_index < 0.8 for p in req.performance_data):
        logger.info("Low performance detected for %s - optimization recommended", req.media_buy_id)

    # Log the update_performance_index call
    audit_logger = get_audit_logger("AdCP", tenant["tenant_id"])
    audit_logger.log_operation(
        operation="update_performance_index",
        principal_name=principal_id or "anonymous",
        principal_id=principal_id or "anonymous",
        adapter_id="mcp_server",
        success=success,
        details={
            "media_buy_id": req.media_buy_id,
            "product_count": len(req.performance_data),
            "avg_performance_index": (
                sum(p.performance_index for p in req.performance_data) / len(req.performance_data)
                if req.performance_data
                else 0
            ),
        },
    )

    return UpdatePerformanceIndexResponse(
        status="success" if success else "failed",
        detail=f"Performance index updated for {len(req.performance_data)} products",
        context=req.context,
    )


def _parse_period_bound(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as e:
        raise AdCPValidationError(f"measurement_period.{name} must be an ISO 8601 date-time") from e


@traced
def _provide_performance_feedback_impl(
    media_buy_id: str,
    performance_index: float,
    measurement_period: dict[str, Any],
    package_id: str | None = None,
    creative_id: str | None = None,
    metric_type: str | None = None,
    feedback_source: str | None = None,
    identity: ResolvedIdentity | None = None,
) -> bool:
    """Record buyer performance feedback for a media buy (AdCP provide_performance_feedback).

    Verifies the caller owns the media buy (and the package, when given),
    forwards the index to the adapter and writes an audit-log entry.

    Raises:
        AdCPAuthenticationError: Missing identity, principal or tenant.
        AdCPMediaBuyNotFoundError / AdCPAuthorizationError: Buy unknown or not owned.
        AdCPInvalidStateError: Imported GAM order not yet claimed via update_media_buy.
        AdCPPackageNotFoundError: ``package_id`` is not part of the media buy.
        AdCPValidationError: Invalid performance_index or measurement_period.
        AdCPAdapterError: The adapter rejected the update.
    """
    if identity is None or not identity.principal_id:
        raise AdCPAuthenticationError("Authentication required for provide_performance_feedback")
    tenant = identity.tenant
    if not tenant:
        raise AdCPAuthenticationError("No tenant context available")

    if performance_index < 0:
        raise AdCPValidationError("performance_index must be >= 0")
    start = _parse_period_bound(measurement_period.get("start"), "start")
    end = _parse_period_bound(measurement_period.get("end"), "end")
    if start > end:
        raise AdCPValidationError("measurement_period.start must be on or before measurement_period.end")

    with MediaBuyUoW(tenant["tenant_id"]) as uow:
        assert uow.media_buys is not None
        assert uow.session is not None
        if media_buy_id in unmaterialized_projected_ids(
            uow.session, tenant["tenant_id"], identity.principal_id, [media_buy_id]
        ):
            raise AdCPInvalidStateError(not_materialized_message(media_buy_id))
        _verify_principal(media_buy_id, identity, uow.media_buys)
        buy_package_ids = [pkg.package_id for pkg in uow.media_buys.get_packages(media_buy_id)]

    if package_id is not None:
        if package_id not in buy_package_ids:
            raise AdCPPackageNotFoundError(f"Package '{package_id}' not found in media buy '{media_buy_id}'.")
        target_package_ids = [package_id]
    else:
        target_package_ids = buy_package_ids

    principal = get_principal_object(identity.principal_id, tenant_id=identity.tenant_id)
    if not principal:
        raise AdCPNotFoundError(f"Principal {identity.principal_id} not found")

    adapter = get_adapter(principal, dry_run=False, tenant=tenant)
    package_performance = [
        PackagePerformance(package_id=pid, performance_index=performance_index) for pid in target_package_ids
    ]
    success = adapter.update_media_buy_performance_index(media_buy_id, package_performance)

    get_audit_logger("AdCP", tenant["tenant_id"]).log_operation(
        operation="provide_performance_feedback",
        principal_name=identity.principal_id,
        principal_id=identity.principal_id,
        adapter_id="mcp_server",
        success=success,
        details={
            "media_buy_id": media_buy_id,
            "package_id": package_id,
            "creative_id": creative_id,
            "performance_index": performance_index,
            "metric_type": metric_type,
            "feedback_source": feedback_source,
            "measurement_period": {"start": start.isoformat(), "end": end.isoformat()},
            "package_ids": target_package_ids,
        },
    )

    if not success:
        raise AdCPAdapterError(f"Adapter rejected performance feedback for media buy '{media_buy_id}'.")
    return True
