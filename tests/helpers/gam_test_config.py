"""Shared GAM line-item config helpers for tests.

Both ``tests/e2e/test_gam_lifecycle.py`` and
``tests/integration/test_gam_real_media_buy_lifecycle.py`` seed products with
the same non-guaranteed CPM line-item shape — extracted here so DRY guard
doesn't fire and so a future GAM API change is fixed in one place.
"""

from __future__ import annotations

from typing import Any


def non_guaranteed_cpm_impl_config(
    targeted_ad_unit_ids: list[str],
    order_name_template: str,
    *,
    width: int = 300,
    height: int = 250,
    priority: int = 12,
) -> dict[str, Any]:
    """Build the implementation_config dict for a non-guaranteed CPM line item.

    Args:
        targeted_ad_unit_ids: GAM ad unit IDs the line item targets.
        order_name_template: Naming-template string (uses {po_number}, {date_range}, etc.).
        width / height: Creative placeholder dimensions.
        priority: GAM line-item priority (12 is the conventional non-guaranteed value).

    Returns:
        Dict suitable for ``Product.implementation_config``.
    """
    return {
        "order_name_template": order_name_template,
        "line_item_type": "PRICE_PRIORITY",
        "priority": priority,
        "cost_type": "CPM",
        "creative_rotation_type": "EVEN",
        "delivery_rate_type": "EVENLY",
        "primary_goal_type": "LIFETIME",
        "primary_goal_unit_type": "IMPRESSIONS",
        "creative_placeholders": [{"width": width, "height": height, "expected_creative_count": 1}],
        "targeted_ad_unit_ids": [str(au) for au in targeted_ad_unit_ids],
    }
