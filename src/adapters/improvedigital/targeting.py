"""Translate AdCP targeting into Improve Digital Classic line-item targeting.

Classic line items carry per-dimension targeting behind dedicated GET/PUT
endpoints (``geo-targeting``, ``device-targeting``, ``time-targeting``,
``browser-targeting``, ``isp-targeting``, ``segment-targeting``,
``pixel-targeting``) plus flat fields on the line item itself
(``size_ids``, frequency caps).

This module emits the line-item-creation subset (inventory selection +
sizes) plus the ``geo_targeting`` list consumed by the live per-line-item
``geo-targeting`` PUT (``LineItemGeoTargetingDto``: ``{"filter": true,
"geo_targeting": [{"country"|"region": ..., "exclude": bool}]}``).

Hard platform constraint: location targeting supports region/country/state/
city (+ up to 10 IP ranges) — **no postal codes**. Postal targeting is
rejected permanently, not "pending".
"""

from __future__ import annotations

import re
import unicodedata
from functools import cache
from typing import Any


def _token(value: Any) -> str:
    """Unwrap adcp RootModel tokens (``.root``) to their plain string."""
    return str(getattr(value, "root", value))


# ISO 3166-1 alpha-2 → the platform's display name, for the countries whose
# 360Yield name diverges from the CLDR English name beyond what
# ``normalize_geo_name`` bridges. Curated empirically against the full live
# dev geo dictionary (2026-08-07): every other assigned code matches via
# babel + normalization.
_GEO_NAME_ALIASES: dict[str, str] = {
    "AN": "Netherland Antilles",  # deprecated ISO code, still on the platform
    "BQ": "Bonaire, Sint Eustatius, and Saba",
    "CD": "DR Congo",
    "CG": "Congo Republic",
    "CI": "Ivory Coast",
    "CV": "Cabo Verde",
    "FM": "Federated States of Micronesia",
    "GS": "South Georgia and the South Sandwich Islands",
    "HK": "Hong Kong",
    "MM": "Myanmar",
    "MO": "Macao",
    "PS": "Palestine",
}


def normalize_geo_name(name: str) -> str:
    """Fold a geo display name for dictionary matching.

    Lowercases, strips diacritics (``São Tomé`` ≡ ``Sao Tome``), folds
    ``&``/punctuation, drops a leading ``the`` (``The Netherlands`` ≡
    ``Netherlands``) and expands ``St.`` → ``Saint`` — the divergences
    observed between CLDR English names and the live 360Yield dictionary.
    """
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(c for c in folded if not unicodedata.combining(c)).lower()
    folded = folded.replace("&", " and ")
    folded = re.sub(r"[^a-z0-9 ]+", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    folded = folded.removeprefix("the ")
    return re.sub(r"\bst\b", "saint", folded)


@cache
def _cldr_country_name(code: str) -> str | None:
    """ISO 3166-1 alpha-2 → CLDR English display name (via babel)."""
    from babel import Locale

    return Locale("en").territories.get(code)


def candidate_country_names(token: str) -> list[str]:
    """Display-name candidates for a country token, most specific first.

    AdCP buyers can only send ISO alpha-2 codes (``GeoCountry`` is
    ``^[A-Z]{2}$``); operators may store either codes or platform names.
    A bare token is tried verbatim; a two-letter token additionally tries
    the curated platform alias and the CLDR English name.
    """
    candidates = [token]
    code = token.strip().upper()
    if len(code) == 2 and code.isalpha():
        alias = _GEO_NAME_ALIASES.get(code)
        if alias:
            candidates.append(alias)
        cldr = _cldr_country_name(code)
        if cldr:
            candidates.append(cldr)
    return candidates


def build_targeting(
    targeting_overlay: Any,
    product_config: dict[str, Any] | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Build the Classic line-item targeting fields for a package.

    Inputs:
        targeting_overlay: AdCP ``Targeting`` model (geo, device, custom).
        product_config: ``ImproveDigitalProductConfig`` as a dict — supplies
            static inventory selection (placements/packages/sizes) and the
            publisher's default geo targeting (``geo_countries`` /
            ``geo_regions`` from the product-config page).
        tenant_id: reserved for future signal resolution; unused for now.

    Returns a dict of line-item field values; only populated dimensions are
    included. ``geo_targeting`` is the union of the product's default geo
    and the buyer's overlay (includes and excludes), deduplicated — the
    create path pops it off the payload and PUTs it to the per-line-item
    ``geo-targeting`` endpoint.
    """
    product_config = product_config or {}
    targeting: dict[str, Any] = {}

    for config_key in ("placement_ids", "excluded_placement_ids", "package_ids", "size_ids"):
        values = product_config.get(config_key)
        if values:
            targeting[config_key] = list(values)

    geo = _geo_entries(targeting_overlay, product_config)
    if geo:
        targeting["geo_targeting"] = geo

    return targeting


def _geo_entries(targeting_overlay: Any, product_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Union of the product's default geo and the buyer's overlay, deduplicated."""
    geo: list[dict[str, Any]] = []
    seen: set[tuple[str, str, bool]] = set()

    def _add(kind: str, value: Any, exclude: bool = False) -> None:
        token = _token(value)
        key = (kind, token, exclude)
        if token and key not in seen:
            seen.add(key)
            # ``exclude`` is required on every entry — the live geo-targeting
            # endpoint 400s with 'missing required properties ["exclude", ...]'
            # when it is omitted, even for plain includes.
            geo.append({kind: token, "exclude": exclude})

    for country in product_config.get("geo_countries") or []:
        _add("country", country)
    for region in product_config.get("geo_regions") or []:
        _add("region", region)

    if targeting_overlay is not None:
        # Overlay geo_regions are deliberately NOT mapped: AdCP GeoRegion
        # tokens are ISO 3166-2 subdivisions (e.g. "US-NY") while the
        # platform's region dimension is continental (APAC/EMEA/…) — the
        # vocabularies cannot meet, so validate_targeting rejects them
        # upfront before any campaign is created.
        for country in getattr(targeting_overlay, "geo_countries", None) or []:
            _add("country", country)
        for country in getattr(targeting_overlay, "geo_countries_exclude", None) or []:
            _add("country", country, exclude=True)

    return geo


def validate_targeting(targeting_overlay: Any) -> list[str]:
    """Return a list of unsupported-targeting messages for Improve Digital.

    Buyers see a clear ``unsupported_targeting`` error rather than have a
    dimension silently dropped at translation time. Dimensions the platform
    does support (dayparting via time-targeting, frequency caps via native
    line-item fields, audiences via segment-targeting) are rejected as
    *pending* until their translation is validated against live credentials
    — shipping an unvalidated wire shape would be worse than an
    explicit error.
    """
    unsupported: list[str] = []
    if targeting_overlay is None:
        return unsupported

    if getattr(targeting_overlay, "geo_postal_areas", None) or getattr(
        targeting_overlay, "geo_postal_areas_exclude", None
    ):
        unsupported.append(
            "Postal-area targeting is not supported on Improve Digital — location targeting "
            "goes down to city level only. Use geo_countries instead."
        )

    if getattr(targeting_overlay, "geo_metros", None) or getattr(targeting_overlay, "geo_metros_exclude", None):
        unsupported.append(
            "Metro/DMA targeting is not supported on Improve Digital — the Classic geo "
            "dimensions are country/region/state/city. Use geo_countries instead."
        )

    if getattr(targeting_overlay, "geo_regions", None) or getattr(targeting_overlay, "geo_regions_exclude", None):
        unsupported.append(
            "Region targeting is not supported on Improve Digital buyer overlays — AdCP "
            "geo_regions are ISO 3166-2 subdivisions (e.g. 'US-NY') but the platform's "
            "region dimension is continental (APAC/EMEA/…), and the platform's state "
            "dimension is pending live validation. Use geo_countries; publishers can set "
            "platform regions on the product configuration."
        )

    if getattr(targeting_overlay, "geo_proximity", None):
        unsupported.append("Proximity (radius) targeting is not supported on Improve Digital.")

    if getattr(targeting_overlay, "frequency_cap", None):
        unsupported.append(
            "Frequency cap targeting pending live validation against the 360Yield API — "
            "set frequency_cap via ImproveDigitalProductConfig for now"
        )

    if getattr(targeting_overlay, "dayparting", None):
        unsupported.append("Dayparting pending live validation against the Classic time-targeting endpoint")

    if getattr(targeting_overlay, "audience_include", None) or getattr(targeting_overlay, "audience_exclude", None):
        unsupported.append("Audience targeting pending segment-targeting signal resolution on Improve Digital")

    return unsupported
