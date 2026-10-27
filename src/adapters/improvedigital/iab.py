"""IAB v1 content categories for Improve Digital Classic creatives.

Every Classic creative carries an IAB category (``CreativeDto.iab_category``
in the rtb/v3 OpenAPI spec; marked mandatory in the Marketplace creative
form and bulk-upload templates). The platform uses the IAB v1 content
taxonomy — values like ``IAB19`` or ``IAB19-6`` (Confluence space ``imp``,
page "IAB Content Category Taxonomy").

AdCP has no IAB field on creatives, so the category is derived from the
buy's ``advertiser_industry`` (AdCP enum, e.g. ``technology.software``),
falling back to the tenant's ``default_iab_category`` connection setting.
"""

from __future__ import annotations

import re
from typing import Any

# IAB1..IAB26, optionally with a sub-category (IAB19-6).
IAB_CATEGORY_PATTERN = re.compile(r"^IAB(?:[1-9]|1\d|2[0-6])(?:-\d{1,2})?$")

# AdCP advertiser-industry enum → IAB v1 category. Sub-industries without
# their own entry fall back to their parent (``finance.banking`` → ``finance``).
# ``cannabis`` is deliberately unmapped: IAB v1 has no fitting category, so
# those buys need an explicit tenant default.
ADVERTISER_INDUSTRY_TO_IAB: dict[str, str] = {
    "automotive": "IAB2",
    "automotive.electric_vehicles": "IAB2-10",
    "automotive.parts_accessories": "IAB2-1",
    "automotive.luxury": "IAB2-13",
    "beauty_cosmetics": "IAB18-1",
    "cpg": "IAB22",
    "dating": "IAB14-1",
    "education": "IAB5",
    "education.online_learning": "IAB5-6",
    "education.k12": "IAB5-1",
    "energy_utilities": "IAB3",
    "energy_utilities.renewable": "IAB3-8",
    "fashion_apparel": "IAB18-3",
    "fashion_apparel.sportswear": "IAB18-5",
    "finance": "IAB13",
    "finance.insurance": "IAB13-6",
    "finance.investment": "IAB13-7",
    "food_beverage": "IAB8",
    "food_beverage.alcohol": "IAB8-5",
    "food_beverage.restaurants": "IAB8-9",
    # Improve Digital files real-money gambling under IAB9-7 (Card Games).
    "gambling_betting": "IAB9-7",
    "gaming": "IAB9-30",
    "government_nonprofit": "IAB11",
    "government_nonprofit.political": "IAB11-4",
    "government_nonprofit.charity": "IAB14",
    "healthcare": "IAB7",
    "home_garden": "IAB10",
    "home_garden.furniture": "IAB10-7",
    "home_garden.home_improvement": "IAB10-9",
    "media_entertainment": "IAB1",
    "media_entertainment.music": "IAB1-6",
    "media_entertainment.film_tv": "IAB1-5",
    "media_entertainment.publishing": "IAB1-1",
    "pets": "IAB16",
    "professional_services": "IAB3",
    "professional_services.legal": "IAB11-2",
    "real_estate": "IAB21",
    "real_estate.residential": "IAB21-3",
    "recruitment_hr": "IAB4",
    "retail": "IAB22",
    "sports_fitness": "IAB17",
    "technology": "IAB19",
    "telecom": "IAB19",
    "telecom.mobile_carriers": "IAB19-6",
    "telecom.internet_providers": "IAB19-18",
    "transportation_logistics": "IAB3-10",
    "travel_hospitality": "IAB20",
    "travel_hospitality.airlines": "IAB20-3",
    "travel_hospitality.hotels": "IAB20-18",
    "travel_hospitality.cruise": "IAB20-12",
}


def iab_category_for_industry(industry: Any) -> str | None:
    """IAB v1 category for an AdCP advertiser industry, or None if unmapped.

    Accepts the AdCP enum (``create_media_buy`` request) or its plain string
    value (adapter asset dicts)."""
    # Kept import-free (no src.core imports): schemas.py imports this module
    # while the adapter package is still initialising.
    value = getattr(industry, "value", None) or getattr(industry, "root", None) or industry
    if not value:
        return None
    value = str(value)
    return ADVERTISER_INDUSTRY_TO_IAB.get(value) or ADVERTISER_INDUSTRY_TO_IAB.get(value.split(".", 1)[0])
