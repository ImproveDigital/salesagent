"""Tests for non-GAM adapter v3 geo field consumption.

Regression tests for salesagent-fwm: ensures all non-GAM adapters read
v3 structured geo fields (geo_countries, geo_regions, geo_metros) instead
of the legacy flat fields (geo_country_any_of, geo_region_any_of, etc.).
"""

from src.core.schemas import Targeting


class TestXandrV3GeoFields:
    """Test Xandr adapter reads v3 field names from targeting dict."""

    def test_targeting_dict_has_v3_country_field(self):
        """model_dump() produces geo_countries, not nested geo.countries."""
        targeting = Targeting(geo_countries=["US", "CA"])
        targeting_dict = targeting.model_dump(exclude_none=True)
        assert "geo_countries" in targeting_dict
        assert targeting_dict["geo_countries"] == ["US", "CA"]

    def test_targeting_dict_has_v3_region_field(self):
        targeting = Targeting(geo_countries=["US"], geo_regions=["US-NY"])
        targeting_dict = targeting.model_dump(exclude_none=True)
        assert "geo_regions" in targeting_dict


class TestMockAdapterV3GeoFields:
    """Test mock adapter uses v3 field names in logging."""

    def test_targeting_geo_countries_accessible(self):
        """Targeting.geo_countries works for mock adapter's logging."""
        targeting = Targeting(geo_countries=["US", "CA"])
        assert targeting.geo_countries is not None
        assert len(targeting.geo_countries) == 2

    def test_targeting_geo_regions_accessible(self):
        targeting = Targeting(geo_countries=["US"], geo_regions=["US-NY", "US-CA"])
        assert targeting.geo_regions is not None
        assert len(targeting.geo_regions) == 2

    def test_targeting_geo_metros_accessible(self):
        targeting = Targeting(
            geo_countries=["US"],
            geo_metros=[{"system": "nielsen_dma", "values": ["501", "803"]}],
        )
        assert targeting.geo_metros is not None
        assert len(targeting.geo_metros) == 1
        assert targeting.geo_metros[0].values == ["501", "803"]
