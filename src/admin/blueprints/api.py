"""API management blueprint."""

import logging
from datetime import UTC, datetime

from flask import Blueprint, jsonify, request
from sqlalchemy import select, text

from src.admin.utils import require_auth
from src.core.database.database_session import get_db_session
from src.core.database.models import Product

logger = logging.getLogger(__name__)

# Create blueprint
api_bp = Blueprint("api", __name__)


# Note: /formats/list route moved to format_search.py blueprint
# (registered at /api/formats/list via format_search_bp)
# This avoids route conflicts and uses the proper async registry pattern


@api_bp.route("/health", methods=["GET"])
def api_health():
    """API health check endpoint."""
    try:
        with get_db_session() as db_session:
            db_session.execute(text("SELECT 1"))
            return jsonify({"status": "healthy"})
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({"status": "unhealthy"}), 500


@api_bp.route("/tenant/<tenant_id>/revenue-chart")
@require_auth()
def revenue_chart_api(tenant_id):
    """API endpoint for the dashboard revenue chart — daily revenue trend for the selected period."""
    from src.admin.services.dashboard_service import DashboardService

    period = (request.args.get("period") or "30d").lower()

    # Parse period into a day count. YTD spans from Jan 1 of the current year.
    if period == "ytd":
        today = datetime.now(UTC).date()
        days = (today - today.replace(month=1, day=1)).days + 1
    else:
        days = {"7d": 7, "30d": 30, "90d": 90}.get(period, 30)

    return jsonify(DashboardService(tenant_id).get_revenue_trend(days))


@api_bp.route("/oauth/status", methods=["GET"])
@require_auth()
def oauth_status():
    """Check if OAuth credentials are properly configured for GAM."""
    try:
        # Check for GAM OAuth credentials using validated configuration
        try:
            from src.core.config import get_gam_oauth_config
            from src.core.logging_config import oauth_structured_logger

            gam_config = get_gam_oauth_config()
            client_id = gam_config.client_id

            # Log configuration check
            oauth_structured_logger.log_gam_oauth_config_load(
                success=True, client_id_prefix=client_id[:20] + "..." if len(client_id) > 20 else client_id
            )

            # Credentials exist and are validated
            return jsonify(
                {
                    "configured": True,
                    "client_id_prefix": client_id[:20] + "..." if len(client_id) > 20 else client_id,
                    "has_secret": bool(gam_config.client_secret),
                    "source": "validated_environment",
                }
            )
        except Exception as config_error:
            # Configuration validation failed
            oauth_structured_logger.log_gam_oauth_config_load(success=False, error=str(config_error))
            return jsonify(
                {
                    "configured": False,
                    "error": f"GAM OAuth configuration error: {str(config_error)}",
                    "help": "Check GAM_OAUTH_CLIENT_ID and GAM_OAUTH_CLIENT_SECRET environment variables.",
                }
            )

    except Exception as e:
        logger.error(f"Error checking OAuth status: {e}")
        return (
            jsonify(
                {
                    "configured": False,
                    "error": f"Error checking OAuth configuration: {str(e)}",
                }
            ),
            500,
        )


@api_bp.route("/tenant/<tenant_id>/products", methods=["GET"])
@require_auth()
def get_tenant_products(tenant_id):
    """API endpoint to list all products for a tenant."""
    try:
        with get_db_session() as db_session:
            from sqlalchemy import select

            from src.core.database.models import Product

            stmt = select(Product).filter_by(tenant_id=tenant_id).order_by(Product.name)
            products = db_session.scalars(stmt).all()

            products_data = []
            for product in products:
                products_data.append(
                    {
                        "product_id": product.product_id,
                        "name": product.name,
                        "description": product.description or "",
                        "delivery_type": product.delivery_type,
                    }
                )

            return jsonify({"products": products_data})

    except Exception as e:
        logger.error(f"Error getting products for tenant {tenant_id}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@api_bp.route("/tenant/<tenant_id>/products/suggestions", methods=["GET"])
@require_auth()
def get_product_suggestions(tenant_id):
    """API endpoint to get product suggestions based on industry and criteria."""
    try:
        from src.services.default_products import (
            get_default_products,
            get_industry_specific_products,
        )

        # Get query parameters
        industry = request.args.get("industry")
        include_standard = request.args.get("include_standard", "true").lower() == "true"
        delivery_type = request.args.get("delivery_type")  # 'guaranteed', 'non_guaranteed', or None for all
        max_cpm = request.args.get("max_cpm", type=float)
        formats = request.args.getlist("formats")  # Can specify multiple format IDs

        # Get suggestions
        suggestions = []

        # Get industry-specific products if industry specified
        if industry:
            industry_products = get_industry_specific_products(industry)
            suggestions.extend(industry_products)
        elif include_standard:
            # If no industry specified but standard requested, get default products
            suggestions.extend(get_default_products())

        # Filter suggestions based on criteria
        filtered_suggestions = []
        for product in suggestions:
            # Filter by delivery type
            if delivery_type and product.get("delivery_type") != delivery_type:
                continue

            # Filter by max CPM
            if max_cpm:
                if product.get("cpm") and product["cpm"] > max_cpm:
                    continue
                if product.get("price_guidance"):
                    if product["price_guidance"]["min"] > max_cpm:
                        continue

            # Filter by formats
            if formats:
                product_formats = set(product.get("formats", []))
                requested_formats = set(formats)
                if not product_formats.intersection(requested_formats):
                    continue

            filtered_suggestions.append(product)

        # Sort suggestions by relevance
        # Prioritize: 1) Industry-specific, 2) Lower CPM, 3) More formats
        def sort_key(product):
            is_industry_specific = product["product_id"] not in [p["product_id"] for p in get_default_products()]
            avg_cpm = (
                product.get("cpm", 0)
                or (product.get("price_guidance", {}).get("min", 0) + product.get("price_guidance", {}).get("max", 0))
                / 2
            )
            format_count = len(product.get("formats", []))
            return (-int(is_industry_specific), avg_cpm, -format_count)

        filtered_suggestions.sort(key=sort_key)

        # Check existing products to mark which are already created
        with get_db_session() as db_session:
            stmt = select(Product.product_id).filter_by(tenant_id=tenant_id)
            existing_ids = set(db_session.scalars(stmt).all())

        # Add metadata to suggestions
        for suggestion in filtered_suggestions:
            suggestion["already_exists"] = suggestion["product_id"] in existing_ids
            suggestion["is_industry_specific"] = suggestion["product_id"] not in [
                p["product_id"] for p in get_default_products()
            ]

            # Calculate match score (0-100)
            score = 100
            if delivery_type and suggestion.get("delivery_type") == delivery_type:
                score += 20
            if formats:
                matching_formats = len(set(suggestion.get("formats", [])).intersection(set(formats)))
                score += matching_formats * 10
            if industry and suggestion["is_industry_specific"]:
                score += 30

            suggestion["match_score"] = min(score, 100)

        return jsonify(
            {
                "suggestions": filtered_suggestions,
                "total_count": len(filtered_suggestions),
                "criteria": {
                    "industry": industry,
                    "delivery_type": delivery_type,
                    "max_cpm": max_cpm,
                    "formats": formats,
                },
            }
        )

    except Exception as e:
        logger.error(f"Error getting product suggestions: {e}")
        return jsonify({"error": str(e)}), 500
