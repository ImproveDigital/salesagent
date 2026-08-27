"""Geo targeting translation for the Improve Digital adapter.

Covers ``build_targeting`` (product-config defaults + buyer overlay →
``LineItemGeoTargetingDto`` entries) and the loud rejection of geo
dimensions the Classic platform cannot express.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.adapters.improvedigital.targeting import (
    build_targeting,
    candidate_country_names,
    normalize_geo_name,
    validate_targeting,
)

pytestmark = pytest.mark.unit


class _Token:
    """Mimics adcp RootModel tokens (value behind ``.root``)."""

    def __init__(self, root: str) -> None:
        self.root = root


class TestBuildGeoTargeting:
    def test_product_config_geo_defaults(self):
        # ``exclude`` is present on every entry, includes too — the live
        # geo-targeting endpoint 400s when it is omitted.
        targeting = build_targeting(None, {"geo_countries": ["NL", "BE"], "geo_regions": ["Flanders"]})

        assert targeting["geo_targeting"] == [
            {"country": "NL", "exclude": False},
            {"country": "BE", "exclude": False},
            {"region": "Flanders", "exclude": False},
        ]

    def test_overlay_adds_on_top_of_config_and_dedupes(self):
        overlay = SimpleNamespace(geo_countries=[_Token("NL"), _Token("DE")], geo_regions=None)
        targeting = build_targeting(overlay, {"geo_countries": ["NL"]})

        assert targeting["geo_targeting"] == [
            {"country": "NL", "exclude": False},
            {"country": "DE", "exclude": False},
        ]

    def test_overlay_excludes_carry_the_exclude_flag(self):
        overlay = SimpleNamespace(
            geo_countries=[_Token("NL")],
            geo_countries_exclude=[_Token("RU")],
        )
        targeting = build_targeting(overlay, {})

        assert targeting["geo_targeting"] == [
            {"country": "NL", "exclude": False},
            {"country": "RU", "exclude": True},
        ]

    def test_overlay_regions_are_not_mapped(self):
        """AdCP geo_regions are ISO 3166-2 subdivisions — the platform's
        continental region dimension cannot express them, so build_targeting
        must not emit region entries from the overlay (validate_targeting
        rejects them upfront)."""
        overlay = SimpleNamespace(geo_regions=[_Token("US-NY")], geo_regions_exclude=[_Token("US-CA")])
        targeting = build_targeting(overlay, {})

        assert "geo_targeting" not in targeting

    def test_no_geo_yields_no_geo_key(self):
        targeting = build_targeting(None, {"placement_ids": [11]})

        assert "geo_targeting" not in targeting
        assert targeting["placement_ids"] == [11]


class TestGeoNameNormalization:
    def test_folds_platform_vs_cldr_divergences(self):
        # The observed divergence classes between CLDR English names and the
        # live 360Yield dictionary — each pair must fold to the same key.
        assert normalize_geo_name("The Netherlands") == normalize_geo_name("Netherlands")
        assert normalize_geo_name("São Tomé and Príncipe") == normalize_geo_name("Sao Tome and Principe")
        assert normalize_geo_name("St. Barthélemy") == normalize_geo_name("Saint Barthelemy")
        assert normalize_geo_name("Ceuta & Melilla") == normalize_geo_name("Ceuta and Melilla")
        assert normalize_geo_name("Côte d'Ivoire") == normalize_geo_name("Cote d Ivoire")

    def test_candidates_for_iso_code_include_cldr_name(self):
        assert candidate_country_names("NL") == ["NL", "Netherlands"]

    def test_candidates_for_aliased_code_prefer_platform_alias(self):
        candidates = candidate_country_names("CI")
        assert candidates[0] == "CI"
        assert candidates[1] == "Ivory Coast"  # curated platform alias wins over CLDR

    def test_non_code_token_passes_verbatim_only(self):
        assert candidate_country_names("The Netherlands") == ["The Netherlands"]


class TestValidateTargeting:
    def test_metro_targeting_rejected_loudly(self):
        overlay = SimpleNamespace(geo_metros=[_Token("501")])
        messages = validate_targeting(overlay)

        assert any("Metro" in m for m in messages)

    def test_proximity_targeting_rejected_loudly(self):
        overlay = SimpleNamespace(geo_proximity=SimpleNamespace(radius=5))
        messages = validate_targeting(overlay)

        assert any("Proximity" in m for m in messages)

    def test_supported_geo_passes_validation(self):
        overlay = SimpleNamespace(geo_countries=[_Token("NL")], geo_countries_exclude=[_Token("RU")])

        assert validate_targeting(overlay) == []

    def test_buyer_region_overlay_rejected_loudly(self):
        """Spec-valid AdCP geo_regions ('US-NY' subdivisions) can never match
        the platform's continental regions — reject before any campaign is
        created, never after."""
        overlay = SimpleNamespace(geo_regions=[_Token("US-NY")])
        messages = validate_targeting(overlay)

        assert any("Region targeting" in m and "geo_countries" in m for m in messages)

    def test_rejection_advice_never_points_at_geo_regions(self):
        """The postal/metro rejection messages must not steer buyers into the
        (unsupported) geo_regions overlay path."""
        overlay = SimpleNamespace(geo_postal_areas=["1012"], geo_metros=[_Token("501")])
        messages = validate_targeting(overlay)

        assert messages, "postal + metro must be rejected"
        assert not any("geo_regions" in m for m in messages)
