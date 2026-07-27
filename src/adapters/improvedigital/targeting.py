"""Translate AdCP targeting into Improve Digital Classic line-item targeting.

Classic line items carry per-dimension targeting behind dedicated GET/PUT
endpoints (``geo-targeting``, ``device-targeting``, ``time-targeting``,
``browser-targeting``, ``isp-targeting``, ``segment-targeting``,
``pixel-targeting``) plus flat fields on the line item itself
(``size_ids``, frequency caps).

This module emits the line-item-creation subset only (inventory selection +
sizes); the per-dimension targeting PUTs land with the M2 buy path. The wire
shapes are exercised by dry-run logging until live calls are validated
against real credentials.

Hard platform constraint: location targeting supports region/country/state/
city (+ up to 10 IP ranges) — **no postal codes**. Postal targeting is
rejected permanently, not "pending".
"""

from __future__ import annotations

from typing import Any


def build_targeting(
    targeting_overlay: Any,
    product_config: dict[str, Any] | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Build the Classic line-item targeting fields for a package.

    Inputs:
        targeting_overlay: AdCP ``Targeting`` model (geo, device, custom).
        product_config: ``ImproveDigitalProductConfig`` as a dict — supplies
            static inventory selection (placements/packages/sizes).
        tenant_id: reserved for signal resolution (M2+); unused for now.

    Returns a dict of line-item field values; only populated dimensions are
    included. Geo lands under ``geo_targeting`` for the dry-run echo — the
    live path PUTs it to the per-line-item ``geo-targeting`` endpoint (M2).
    """
    product_config = product_config or {}
    targeting: dict[str, Any] = {}

    for config_key in ("placement_ids", "excluded_placement_ids", "package_ids", "size_ids"):
        values = product_config.get(config_key)
        if values:
            targeting[config_key] = list(values)

    if targeting_overlay is not None:
        geo: list[dict[str, Any]] = []
        if getattr(targeting_overlay, "geo_countries", None):
            geo.extend({"country": c.root} for c in targeting_overlay.geo_countries)
        if getattr(targeting_overlay, "geo_regions", None):
            geo.extend({"region": r.root} for r in targeting_overlay.geo_regions)
        if geo:
            targeting["geo_targeting"] = geo

    return targeting


def validate_targeting(targeting_overlay: Any) -> list[str]:
    """Return a list of unsupported-targeting messages for Improve Digital.

    Buyers see a clear ``unsupported_targeting`` error rather than have a
    dimension silently dropped at translation time. Dimensions the platform
    does support (dayparting via time-targeting, frequency caps via native
    line-item fields, audiences via segment-targeting) are rejected as
    *pending* until their translation is validated against live credentials
    in M2 — shipping an unvalidated wire shape would be worse than an
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
            "goes down to city level only. Use geo_regions or geo_countries instead."
        )

    if getattr(targeting_overlay, "frequency_cap", None):
        unsupported.append(
            "Frequency cap targeting pending live validation against the 360Yield API — "
            "set frequency_cap via ImproveDigitalProductConfig for now"
        )

    if getattr(targeting_overlay, "dayparting", None):
        unsupported.append("Dayparting pending live validation against the Classic time-targeting endpoint (M2)")

    if getattr(targeting_overlay, "audience_include", None) or getattr(targeting_overlay, "audience_exclude", None):
        unsupported.append("Audience targeting pending segment-targeting signal resolution on Improve Digital (M2+)")

    return unsupported
