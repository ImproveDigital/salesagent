"""Unit tests for get_adcp_capabilities tool.

Tests the capabilities endpoint that returns what this sales agent supports
per the AdCP spec.
"""

from __future__ import annotations

import pytest
from adcp.types import GetAdcpCapabilitiesResponse
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
    SupportedProtocol,
)


class TestGetAdcpCapabilitiesSchema:
    """Test GetAdcpCapabilitiesResponse schema validation."""

    def test_response_requires_adcp_field(self):
        """Test that response requires adcp field."""
        # Must have adcp and supported_protocols per spec
        with pytest.raises(ValueError):
            GetAdcpCapabilitiesResponse(supported_protocols=[SupportedProtocol.media_buy])

    def test_response_requires_supported_protocols(self):
        """Test that response requires supported_protocols field."""
        from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
            Adcp,
            Idempotency,
            MajorVersion,
        )

        # Must have supported_protocols (non-empty list)
        with pytest.raises(ValueError):
            GetAdcpCapabilitiesResponse(
                adcp=Adcp(
                    major_versions=[MajorVersion(root=3)],
                    idempotency=Idempotency(supported=True, replay_ttl_seconds=86400),
                ),
                supported_protocols=[],  # Empty not allowed
            )

    def test_valid_minimal_response(self):
        """Test creating a valid minimal response."""
        from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
            Adcp,
            Idempotency,
            MajorVersion,
        )

        response = GetAdcpCapabilitiesResponse(
            adcp=Adcp(
                major_versions=[MajorVersion(root=3)],
                idempotency=Idempotency(supported=True, replay_ttl_seconds=86400),
            ),
            supported_protocols=[SupportedProtocol.media_buy],
        )

        assert response.adcp is not None
        assert len(response.adcp.major_versions) == 1
        assert response.adcp.major_versions[0].root == 3
        assert SupportedProtocol.media_buy in response.supported_protocols

    def test_response_with_media_buy_capabilities(self):
        """Test creating response with media_buy capabilities."""
        from adcp.types.generated_poc.core.media_buy_features import MediaBuyFeatures
        from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
            Adcp,
            Execution,
            Idempotency,
            MajorVersion,
            MediaBuy,
            Portfolio,
            PublisherDomain,
            Targeting,
        )

        response = GetAdcpCapabilitiesResponse(
            adcp=Adcp(
                major_versions=[MajorVersion(root=3)],
                idempotency=Idempotency(supported=True, replay_ttl_seconds=86400),
            ),
            supported_protocols=[SupportedProtocol.media_buy],
            media_buy=MediaBuy(
                portfolio=Portfolio(
                    description="Test portfolio",
                    publisher_domains=[PublisherDomain(root="example.com")],
                ),
                features=MediaBuyFeatures(
                    content_standards=True,
                    inline_creative_management=True,
                    property_list_filtering=True,
                ),
                execution=Execution(
                    targeting=Targeting(
                        geo_countries=True,
                        geo_regions=True,
                    ),
                ),
            ),
        )

        assert response.media_buy is not None
        assert response.media_buy.portfolio is not None
        assert len(response.media_buy.portfolio.publisher_domains) == 1
        assert response.media_buy.features is not None
        assert response.media_buy.features.content_standards is True


class TestGetAdcpCapabilitiesImports:
    """Test that get_adcp_capabilities can be imported correctly."""

    def test_capabilities_module_imports(self):
        """Test that the capabilities module can be imported."""
        from src.core.tools import capabilities

        assert capabilities is not None
