"""
GAM Reporting API endpoints

Provides REST API endpoints for accessing GAM reporting data with:
- Spend and impression numbers by advertiser, order, and line item
- Three date range options: lifetime, this month, today
- Timezone handling and data freshness timestamps
"""

import logging
import re
from functools import wraps
from typing import Literal, cast

import pytz
from flask import Blueprint, jsonify, request, session
from sqlalchemy import select

from scripts.ops.gam_helper import get_ad_manager_client_for_tenant
from src.adapters.gam_reporting_service import GAMReportingService
from src.core.database.database_session import get_db_session
from src.core.database.models import AdapterConfig, Principal, Tenant

logger = logging.getLogger(__name__)

# Create Blueprint
gam_reporting_api = Blueprint("gam_reporting_api", __name__)

# System default timezone for reporting (Azerion HQ)
DEFAULT_REPORTING_TIMEZONE = "Europe/Amsterdam"

# Input validation patterns
TENANT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
PRINCIPAL_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
ID_PATTERN = re.compile(r"^\d+$")  # For advertiser_id, order_id, line_item_id


def validate_tenant_id(tenant_id: str) -> bool:
    """Validate tenant ID format"""
    return bool(TENANT_ID_PATTERN.match(tenant_id)) and len(tenant_id) <= 100


def validate_principal_id(principal_id: str) -> bool:
    """Validate principal ID format"""
    return bool(PRINCIPAL_ID_PATTERN.match(principal_id)) and len(principal_id) <= 100


def validate_numeric_id(id_str: str) -> bool:
    """Validate numeric ID format"""
    return bool(ID_PATTERN.match(id_str)) and len(id_str) <= 20


def validate_timezone(tz_str: str) -> bool:
    """Validate timezone string"""
    try:
        pytz.timezone(tz_str)
        return True
    except pytz.exceptions.UnknownTimeZoneError:
        return False


def require_auth(f):
    """Decorator to require authentication for API endpoints.

    This is a specialized version for API endpoints that returns JSON 401 responses
    instead of redirects. It uses the same session checking as the main admin UI.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        from flask import request

        # Check if user is authenticated (align with admin_ui style)
        # Admin UI sets session["user"] on successful authentication
        has_user = "user" in session
        logger.debug(
            f"GAM API auth check - path: {request.path}, session keys: {list(session.keys())}, has_user: {has_user}"
        )

        if not has_user:
            logger.warning(
                f"GAM API authentication failed - no user in session. Path: {request.path}, Session keys: {list(session.keys())}"
            )
            return jsonify({"error": "Authentication required. Please log in to the admin UI."}), 401

        return f(*args, **kwargs)

    return decorated_function


def get_tenant_access(tenant_id: str) -> bool:
    """Check if the current user has access to the specified tenant"""
    # Handle admin_ui style sessions
    # Super admin has access to all tenants
    if session.get("role") == "super_admin":
        return True

    # Check if user has access to this specific tenant
    if session.get("tenant_id") == tenant_id:
        return True

    # Handle legacy user object style (if any)
    user = session.get("user", {})
    if isinstance(user, dict):
        if user.get("is_super_admin"):
            return True
        user_tenants = user.get("tenants", [])
        return tenant_id in user_tenants

    return False


# Media buy statuses eligible for a delivery-metrics snapshot update.
_DELIVERY_SYNC_STATUSES = ("active", "approved", "completed")


def _persist_media_buy_delivery_snapshots(tenant_id: str, rows: list[dict]) -> None:
    """Persist all-time delivery totals onto media buys matched by GAM order ID.

    Aggregates report rows per order and updates ``delivered_amount`` /
    ``delivered_impressions`` / ``delivery_synced_at`` so dashboards read
    fresh delivery data without a visit to each media buy detail page.
    Only call this with all-time rows — partial ranges would understate
    lifetime delivery.
    """
    from datetime import UTC, datetime
    from decimal import Decimal

    from src.core.database.repositories.media_buy import MediaBuyRepository

    totals: dict[str, dict[str, float]] = {}
    for row in rows:
        order_id = str(row.get("order_id") or "").strip()
        if not order_id:
            continue
        agg = totals.setdefault(order_id, {"impressions": 0, "revenue_micros": 0.0})
        agg["impressions"] += int(row.get("impressions") or 0)
        # Sum full-precision micros so per-row 2-dp rounding of "spend"
        # doesn't compound across line items.
        revenue_micros = row.get("revenue_micros")
        if revenue_micros is not None:
            agg["revenue_micros"] += float(revenue_micros)
        else:
            agg["revenue_micros"] += float(row.get("spend") or 0) * 1_000_000

    if not totals:
        return

    synced_at = datetime.now(UTC)
    with get_db_session() as db_session:
        repo = MediaBuyRepository(db_session, tenant_id)
        for order_id, agg in totals.items():
            media_buy = repo.get_by_external_id(order_id)
            if not media_buy or media_buy.status not in _DELIVERY_SYNC_STATUSES:
                continue
            repo.update_fields(
                media_buy.media_buy_id,
                delivered_amount=Decimal(str(round(agg["revenue_micros"] / 1_000_000, 2))),
                delivered_impressions=int(agg["impressions"]),
                delivery_synced_at=synced_at,
            )
        db_session.commit()


@gam_reporting_api.route("/api/tenant/<tenant_id>/gam/reporting", methods=["GET"])
@require_auth
def get_gam_reporting(tenant_id: str):
    """
    Get GAM reporting data

    Query parameters:
    - date_range: "lifetime", "this_month", "today", or "all_time" (required)
    - advertiser_id: Filter by advertiser ID (optional)
    - order_id: Filter by order ID (optional)
    - line_item_id: Filter by line item ID (optional)
    - timezone: Requested timezone (default: Europe/Amsterdam)
    """
    # Validate tenant_id
    if not validate_tenant_id(tenant_id):
        return jsonify({"error": "Invalid tenant ID format"}), 400

    # Check access
    if not get_tenant_access(tenant_id):
        return jsonify({"error": "Access denied to this tenant"}), 403

    # Check if tenant is using GAM
    with get_db_session() as db_session:
        stmt = select(Tenant).filter_by(tenant_id=tenant_id)
        tenant = db_session.scalars(stmt).first()

    if not tenant or tenant.ad_server != "google_ad_manager":
        return jsonify({"error": "GAM reporting is only available for tenants using Google Ad Manager"}), 400

    # Get query parameters
    date_range = request.args.get("date_range")
    if not date_range or date_range not in ["lifetime", "this_month", "today", "all_time"]:
        return jsonify(
            {"error": "Invalid or missing date_range. Must be one of: lifetime, this_month, today, all_time"}
        ), 400

    # Validate optional numeric IDs
    advertiser_id = request.args.get("advertiser_id")
    if advertiser_id and not validate_numeric_id(advertiser_id):
        return jsonify({"error": "Invalid advertiser_id format"}), 400

    order_id = request.args.get("order_id")
    if order_id and not validate_numeric_id(order_id):
        return jsonify({"error": "Invalid order_id format"}), 400

    line_item_id = request.args.get("line_item_id")
    if line_item_id and not validate_numeric_id(line_item_id):
        return jsonify({"error": "Invalid line_item_id format"}), 400

    # Validate timezone
    timezone = request.args.get("timezone", DEFAULT_REPORTING_TIMEZONE)
    if not validate_timezone(timezone):
        return jsonify({"error": "Invalid timezone"}), 400

    try:
        # Get the GAM client for this tenant
        logger.info(f"Getting GAM client for tenant {tenant_id}")
        gam_client = get_ad_manager_client_for_tenant(tenant_id)

        if not gam_client:
            logger.error(f"Failed to get GAM client for tenant {tenant_id}")
            return jsonify({"error": "GAM client not configured for this tenant"}), 500

        # Get the network timezone (fetching if necessary)
        from scripts.ops.gam_helper import ensure_network_timezone

        logger.info(f"Getting network timezone for tenant {tenant_id}")
        network_timezone = ensure_network_timezone(tenant_id)

        # Create reporting service
        logger.info(f"Creating reporting service for tenant {tenant_id}")
        reporting_service = GAMReportingService(gam_client, network_timezone)

        # Get the reporting data
        logger.info(f"Getting reporting data for tenant {tenant_id}, date_range={date_range}")
        # Type narrowing: We validated date_range above
        date_range_literal = cast(Literal["lifetime", "this_month", "today", "all_time"], date_range)
        report_data = reporting_service.get_reporting_data(
            date_range=date_range_literal,
            advertiser_id=advertiser_id,
            order_id=order_id,
            line_item_id=line_item_id,
            requested_timezone=timezone,
        )

        # Update media buy delivery fields from the fetched rows. Only
        # all_time rows cover the full delivery window (today/this_month/
        # lifetime are partial and would understate delivered totals).
        # Best-effort: a persistence failure must not break the report.
        if date_range_literal == "all_time":
            try:
                _persist_media_buy_delivery_snapshots(tenant_id, report_data.data)
            except Exception as persist_err:
                logger.warning(f"Failed to persist delivery snapshots for tenant {tenant_id}: {persist_err}")

        # Convert to JSON-serializable format
        response = {
            "success": True,
            "data": report_data.data,
            "metadata": {
                "start_date": report_data.start_date.isoformat(),
                "end_date": report_data.end_date.isoformat(),
                "requested_timezone": report_data.requested_timezone,
                "data_timezone": report_data.data_timezone,
                "data_valid_until": report_data.data_valid_until.isoformat(),
                "query_type": report_data.query_type,
                "dimensions": report_data.dimensions,
                "metrics": report_data.metrics,
            },
        }

        return jsonify(response)

    except Exception as e:
        logger.error(f"Error getting GAM reporting data for tenant {tenant_id}: {str(e)}", exc_info=True)
        return jsonify({"error": f"Failed to get reporting data: {str(e)}"}), 500


@gam_reporting_api.route("/api/tenant/<tenant_id>/gam/reporting/advertiser/<advertiser_id>/summary", methods=["GET"])
@require_auth
def get_advertiser_summary(tenant_id: str, advertiser_id: str):
    """
    Get summary reporting data for a specific advertiser

    Query parameters:
    - date_range: "lifetime", "this_month", "today", or "all_time" (required)
    - timezone: Requested timezone (default: Europe/Amsterdam)
    """
    # Validate IDs
    if not validate_tenant_id(tenant_id):
        return jsonify({"error": "Invalid tenant ID format"}), 400
    if not validate_numeric_id(advertiser_id):
        return jsonify({"error": "Invalid advertiser ID format"}), 400

    # Check access
    if not get_tenant_access(tenant_id):
        return jsonify({"error": "Access denied to this tenant"}), 403

    # Check if tenant is using GAM
    with get_db_session() as db_session:
        stmt = select(Tenant).filter_by(tenant_id=tenant_id)
        tenant = db_session.scalars(stmt).first()

    if not tenant or tenant.ad_server != "google_ad_manager":
        return jsonify({"error": "GAM reporting is only available for tenants using Google Ad Manager"}), 400

    # Get query parameters
    date_range = request.args.get("date_range")
    if not date_range or date_range not in ["lifetime", "this_month", "today", "all_time"]:
        return jsonify(
            {"error": "Invalid or missing date_range. Must be one of: lifetime, this_month, today, all_time"}
        ), 400

    timezone = request.args.get("timezone", DEFAULT_REPORTING_TIMEZONE)
    if not validate_timezone(timezone):
        return jsonify({"error": "Invalid timezone"}), 400

    try:
        # Get the GAM client for this tenant
        gam_client = get_ad_manager_client_for_tenant(tenant_id)

        # Get the network timezone (fetching if necessary)
        from scripts.ops.gam_helper import ensure_network_timezone

        network_timezone = ensure_network_timezone(tenant_id)

        # Create reporting service
        reporting_service = GAMReportingService(gam_client, network_timezone)

        # Get the advertiser summary
        # Type narrowing: We validated date_range above
        date_range_literal = cast(Literal["lifetime", "this_month", "today", "all_time"], date_range)
        summary = reporting_service.get_advertiser_summary(
            advertiser_id=advertiser_id, date_range=date_range_literal, requested_timezone=timezone
        )

        return jsonify({"success": True, "data": summary})

    except Exception as e:
        logger.error(f"Error getting advertiser summary: {str(e)}")
        return jsonify({"error": f"Failed to get advertiser summary: {str(e)}"}), 500


@gam_reporting_api.route("/api/tenant/<tenant_id>/principals/<principal_id>/gam/reporting", methods=["GET"])
@require_auth
def get_principal_reporting(tenant_id: str, principal_id: str):
    """
    Get GAM reporting data for a specific principal (advertiser)
    This endpoint automatically uses the principal's configured advertiser_id

    Query parameters:
    - date_range: "lifetime", "this_month", "today", or "all_time" (required)
    - order_id: Filter by order ID (optional)
    - line_item_id: Filter by line item ID (optional)
    - timezone: Requested timezone (default: Europe/Amsterdam)
    """
    # Validate IDs
    if not validate_tenant_id(tenant_id):
        return jsonify({"error": "Invalid tenant ID format"}), 400
    if not validate_principal_id(principal_id):
        return jsonify({"error": "Invalid principal ID format"}), 400

    # Check access
    if not get_tenant_access(tenant_id):
        return jsonify({"error": "Access denied to this tenant"}), 403

    # Get the principal's advertiser_id
    with get_db_session() as db_session:
        stmt = select(Principal).filter_by(tenant_id=tenant_id, principal_id=principal_id)
        principal = db_session.scalars(stmt).first()

    if not principal:
        return jsonify({"error": "Principal not found"}), 404

    import json

    platform_mappings = principal.platform_mappings
    if isinstance(platform_mappings, str):
        platform_mappings = json.loads(platform_mappings)
    advertiser_id = platform_mappings.get("gam_advertiser_id")

    if not advertiser_id:
        return jsonify({"error": "Principal does not have a GAM advertiser ID configured"}), 400

    # Get query parameters
    date_range = request.args.get("date_range")
    if not date_range or date_range not in ["lifetime", "this_month", "today", "all_time"]:
        return jsonify(
            {"error": "Invalid or missing date_range. Must be one of: lifetime, this_month, today, all_time"}
        ), 400

    # Validate optional numeric IDs
    order_id = request.args.get("order_id")
    if order_id and not validate_numeric_id(order_id):
        return jsonify({"error": "Invalid order_id format"}), 400

    line_item_id = request.args.get("line_item_id")
    if line_item_id and not validate_numeric_id(line_item_id):
        return jsonify({"error": "Invalid line_item_id format"}), 400

    # Validate timezone
    timezone = request.args.get("timezone", DEFAULT_REPORTING_TIMEZONE)
    if not validate_timezone(timezone):
        return jsonify({"error": "Invalid timezone"}), 400

    try:
        # Get the GAM client for this tenant
        gam_client = get_ad_manager_client_for_tenant(tenant_id)

        # Get the network timezone from adapter config
        with get_db_session() as db_session:
            stmt_config = select(AdapterConfig).filter_by(tenant_id=tenant_id, adapter_type="google_ad_manager")
            adapter_config = db_session.scalars(stmt_config).first()

        if not adapter_config:
            # Default to the system timezone if no config found
            network_timezone = DEFAULT_REPORTING_TIMEZONE
        else:
            # TODO: Add gam_network_timezone field to adapter_config table if timezone configuration is needed
            # For now, use default timezone since config field no longer exists
            network_timezone = DEFAULT_REPORTING_TIMEZONE

        # Create reporting service
        reporting_service = GAMReportingService(gam_client, network_timezone)

        # Get the reporting data
        # Type narrowing: We validated date_range above
        date_range_literal = cast(Literal["lifetime", "this_month", "today", "all_time"], date_range)
        report_data = reporting_service.get_reporting_data(
            date_range=date_range_literal,
            advertiser_id=advertiser_id,
            order_id=order_id,
            line_item_id=line_item_id,
            requested_timezone=timezone,
        )

        # Convert to JSON-serializable format
        response = {
            "success": True,
            "principal_id": principal_id,
            "advertiser_id": advertiser_id,
            "data": report_data.data,
            "metadata": {
                "start_date": report_data.start_date.isoformat(),
                "end_date": report_data.end_date.isoformat(),
                "requested_timezone": report_data.requested_timezone,
                "data_timezone": report_data.data_timezone,
                "data_valid_until": report_data.data_valid_until.isoformat(),
                "query_type": report_data.query_type,
                "dimensions": report_data.dimensions,
                "metrics": report_data.metrics,
            },
        }

        return jsonify(response)

    except Exception as e:
        logger.error(f"Error getting principal reporting data: {str(e)}")
        return jsonify({"error": f"Failed to get reporting data: {str(e)}"}), 500


@gam_reporting_api.route("/api/tenant/<tenant_id>/gam/reporting/countries", methods=["GET"])
@require_auth
def get_country_breakdown(tenant_id: str):
    """
    Get GAM reporting data broken down by country

    Query parameters:
    - date_range: "lifetime", "this_month", "today", or "all_time" (required)
    - advertiser_id: Filter by advertiser ID (optional)
    - order_id: Filter by order ID (optional)
    - line_item_id: Filter by line item ID (optional)
    - timezone: Requested timezone (default: Europe/Amsterdam)
    """
    # Validate tenant_id
    if not validate_tenant_id(tenant_id):
        return jsonify({"error": "Invalid tenant ID format"}), 400

    # Check access
    if not get_tenant_access(tenant_id):
        return jsonify({"error": "Access denied to this tenant"}), 403

    # Check if tenant is using GAM
    with get_db_session() as db_session:
        stmt = select(Tenant).filter_by(tenant_id=tenant_id)
        tenant = db_session.scalars(stmt).first()

    if not tenant or tenant.ad_server != "google_ad_manager":
        return jsonify({"error": "GAM reporting is only available for tenants using Google Ad Manager"}), 400

    # Get query parameters
    date_range = request.args.get("date_range")
    if not date_range or date_range not in ["lifetime", "this_month", "today", "all_time"]:
        return jsonify(
            {"error": "Invalid or missing date_range. Must be one of: lifetime, this_month, today, all_time"}
        ), 400

    # Validate optional numeric IDs
    advertiser_id = request.args.get("advertiser_id")
    if advertiser_id and not validate_numeric_id(advertiser_id):
        return jsonify({"error": "Invalid advertiser_id format"}), 400

    order_id = request.args.get("order_id")
    if order_id and not validate_numeric_id(order_id):
        return jsonify({"error": "Invalid order_id format"}), 400

    line_item_id = request.args.get("line_item_id")
    if line_item_id and not validate_numeric_id(line_item_id):
        return jsonify({"error": "Invalid line_item_id format"}), 400

    # Validate timezone
    timezone = request.args.get("timezone", DEFAULT_REPORTING_TIMEZONE)
    if not validate_timezone(timezone):
        return jsonify({"error": "Invalid timezone"}), 400

    try:
        # Get the GAM client for this tenant
        gam_client = get_ad_manager_client_for_tenant(tenant_id)

        if not gam_client:
            return jsonify({"error": "GAM client not configured for this tenant"}), 500

        # Get the network timezone
        from scripts.ops.gam_helper import ensure_network_timezone

        network_timezone = ensure_network_timezone(tenant_id)

        # Create reporting service
        reporting_service = GAMReportingService(gam_client, network_timezone)

        # Get the country breakdown
        # Type narrowing: We validated date_range above
        date_range_literal = cast(Literal["lifetime", "this_month", "today", "all_time"], date_range)
        country_data = reporting_service.get_country_breakdown(
            date_range=date_range_literal,
            advertiser_id=advertiser_id,
            order_id=order_id,
            line_item_id=line_item_id,
            requested_timezone=timezone,
        )

        return jsonify({"success": True, "data": country_data})

    except Exception as e:
        logger.error(f"Error getting country breakdown: {str(e)}", exc_info=True)
        return jsonify({"error": f"Failed to get country breakdown: {str(e)}"}), 500


@gam_reporting_api.route("/api/tenant/<tenant_id>/gam/reporting/ad-units", methods=["GET"])
@require_auth
def get_ad_unit_breakdown(tenant_id: str):
    """
    Get GAM reporting data broken down by ad unit

    Query parameters:
    - date_range: "lifetime", "this_month", "today", or "all_time" (required)
    - advertiser_id: Filter by advertiser ID (optional)
    - order_id: Filter by order ID (optional)
    - line_item_id: Filter by line item ID (optional)
    - country: Filter by country name (optional)
    - timezone: Requested timezone (default: Europe/Amsterdam)
    """
    # Validate tenant_id
    if not validate_tenant_id(tenant_id):
        return jsonify({"error": "Invalid tenant ID format"}), 400

    # Check access
    if not get_tenant_access(tenant_id):
        return jsonify({"error": "Access denied to this tenant"}), 403

    # Check if tenant is using GAM
    with get_db_session() as db_session:
        stmt = select(Tenant).filter_by(tenant_id=tenant_id)
        tenant = db_session.scalars(stmt).first()

    if not tenant or tenant.ad_server != "google_ad_manager":
        return jsonify({"error": "GAM reporting is only available for tenants using Google Ad Manager"}), 400

    # Get query parameters
    date_range = request.args.get("date_range")
    if not date_range or date_range not in ["lifetime", "this_month", "today", "all_time"]:
        return jsonify(
            {"error": "Invalid or missing date_range. Must be one of: lifetime, this_month, today, all_time"}
        ), 400

    # Validate optional numeric IDs
    advertiser_id = request.args.get("advertiser_id")
    if advertiser_id and not validate_numeric_id(advertiser_id):
        return jsonify({"error": "Invalid advertiser_id format"}), 400

    order_id = request.args.get("order_id")
    if order_id and not validate_numeric_id(order_id):
        return jsonify({"error": "Invalid order_id format"}), 400

    line_item_id = request.args.get("line_item_id")
    if line_item_id and not validate_numeric_id(line_item_id):
        return jsonify({"error": "Invalid line_item_id format"}), 400

    # Get country filter (string, not numeric)
    country = request.args.get("country")

    # Validate timezone
    timezone = request.args.get("timezone", DEFAULT_REPORTING_TIMEZONE)
    if not validate_timezone(timezone):
        return jsonify({"error": "Invalid timezone"}), 400

    try:
        # Get the GAM client for this tenant
        gam_client = get_ad_manager_client_for_tenant(tenant_id)

        if not gam_client:
            return jsonify({"error": "GAM client not configured for this tenant"}), 500

        # Get the network timezone
        from scripts.ops.gam_helper import ensure_network_timezone

        network_timezone = ensure_network_timezone(tenant_id)

        # Create reporting service
        reporting_service = GAMReportingService(gam_client, network_timezone)

        # Get the ad unit breakdown
        # Type narrowing: We validated date_range above
        date_range_literal = cast(Literal["lifetime", "this_month", "today", "all_time"], date_range)
        ad_unit_data = reporting_service.get_ad_unit_breakdown(
            date_range=date_range_literal,
            advertiser_id=advertiser_id,
            order_id=order_id,
            line_item_id=line_item_id,
            country=country,
            requested_timezone=timezone,
        )

        return jsonify({"success": True, "data": ad_unit_data})

    except Exception as e:
        logger.error(f"Error getting ad unit breakdown: {str(e)}", exc_info=True)
        return jsonify({"error": f"Failed to get ad unit breakdown: {str(e)}"}), 500


@gam_reporting_api.route("/api/tenant/<tenant_id>/principals/<principal_id>/gam/reporting/summary", methods=["GET"])
@require_auth
def get_principal_summary(tenant_id: str, principal_id: str):
    """
    Get summary reporting data for a specific principal (advertiser)

    Query parameters:
    - date_range: "lifetime", "this_month", "today", or "all_time" (required)
    - timezone: Requested timezone (default: Europe/Amsterdam)
    """
    # Validate IDs
    if not validate_tenant_id(tenant_id):
        return jsonify({"error": "Invalid tenant ID format"}), 400
    if not validate_principal_id(principal_id):
        return jsonify({"error": "Invalid principal ID format"}), 400

    # Check access
    if not get_tenant_access(tenant_id):
        return jsonify({"error": "Access denied to this tenant"}), 403

    # Get the principal's advertiser_id
    with get_db_session() as db_session:
        stmt = select(Principal).filter_by(tenant_id=tenant_id, principal_id=principal_id)
        principal = db_session.scalars(stmt).first()

    if not principal:
        return jsonify({"error": "Principal not found"}), 404

    import json

    platform_mappings = principal.platform_mappings
    if isinstance(platform_mappings, str):
        platform_mappings = json.loads(platform_mappings)
    advertiser_id = platform_mappings.get("gam_advertiser_id")

    if not advertiser_id:
        return jsonify({"error": "Principal does not have a GAM advertiser ID configured"}), 400

    # Get query parameters
    date_range = request.args.get("date_range")
    if not date_range or date_range not in ["lifetime", "this_month", "today", "all_time"]:
        return jsonify(
            {"error": "Invalid or missing date_range. Must be one of: lifetime, this_month, today, all_time"}
        ), 400

    timezone = request.args.get("timezone", DEFAULT_REPORTING_TIMEZONE)
    if not validate_timezone(timezone):
        return jsonify({"error": "Invalid timezone"}), 400

    try:
        # Get the GAM client for this tenant
        gam_client = get_ad_manager_client_for_tenant(tenant_id)

        # Get the network timezone from adapter config
        with get_db_session() as db_session:
            stmt_config = select(AdapterConfig).filter_by(tenant_id=tenant_id, adapter_type="google_ad_manager")
            adapter_config = db_session.scalars(stmt_config).first()

        if not adapter_config:
            # Default to the system timezone if no config found
            network_timezone = DEFAULT_REPORTING_TIMEZONE
        else:
            # TODO: Add gam_network_timezone field to adapter_config table if timezone configuration is needed
            # For now, use default timezone since config field no longer exists
            network_timezone = DEFAULT_REPORTING_TIMEZONE

        # Create reporting service
        reporting_service = GAMReportingService(gam_client, network_timezone)

        # Get the advertiser summary
        # Type narrowing: We validated date_range above
        date_range_literal = cast(Literal["lifetime", "this_month", "today", "all_time"], date_range)
        summary = reporting_service.get_advertiser_summary(
            advertiser_id=advertiser_id, date_range=date_range_literal, requested_timezone=timezone
        )

        # Add principal info to the response
        summary["principal_id"] = principal_id

        return jsonify({"success": True, "data": summary})

    except Exception as e:
        logger.error(f"Error getting principal summary: {str(e)}")
        return jsonify({"error": f"Failed to get principal summary: {str(e)}"}), 500
