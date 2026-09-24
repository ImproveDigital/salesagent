"""Behavioral snapshot tests for UC-005 (creative format discovery).

These tests capture the behavioral contract of _list_creative_formats_impl
as specified by BDD scenarios from BR-UC-005. They serve as a migration safety
net: if any refactoring (e.g., FastAPI migration) silently changes behavior,
these tests will catch it.

Each test references its upstream BDD scenario ID for traceability.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from adcp.types.generated_poc.core.format import Assets, Dimensions, Renders
from adcp.types.generated_poc.core.format import Assets10 as Assets5

# adcp 7: Assets classes are type-discriminated by asset_type + item_type.
# Assets = individual image, Assets10 (aliased Assets5 here) = individual video
# Assets24 = repeatable_group (has nested assets, no asset_type)
# Nested group assets (Assets25 union): Assets26 (image), Assets27 (video), Assets29 (text), etc.
from src.core.schemas import Format, FormatId, ListCreativeFormatsRequest
from tests.factories import PrincipalFactory

DEFAULT_AGENT_URL = "https://creative.adcontextprotocol.org"
MOCK_TENANT = {"tenant_id": "test-tenant", "name": "Test Tenant"}


def _make_format(
    format_id: str,
    name: str,
    renders: list | None = None,
    assets: list | None = None,
) -> Format:
    """Helper to create a Format object with minimal boilerplate."""
    return Format(
        format_id=FormatId(agent_url=DEFAULT_AGENT_URL, id=format_id),
        name=name,
        is_standard=True,
        renders=renders,
        assets=assets,
    )


def _call_impl(
    formats: list[Format],
    req: ListCreativeFormatsRequest | None = None,
) -> list[Format]:
    """Call _list_creative_formats_impl with mocked dependencies.

    Returns the list of formats from the response. Mocks tenant,
    creative agent registry, DB session (for broadstreet check), and
    audit logger so the test exercises only the filtering/sorting logic.
    Identity is passed directly as a ResolvedIdentity.
    """
    from src.core.creative_agent_registry import FormatFetchResult
    from src.core.tools.creative_formats import _list_creative_formats_impl

    if req is None:
        req = ListCreativeFormatsRequest()

    identity = PrincipalFactory.make_identity(
        principal_id=None,
        tenant_id=MOCK_TENANT["tenant_id"],
        tenant=MOCK_TENANT,
    )

    with (
        patch("src.core.creative_agent_registry.get_creative_agent_registry") as mock_registry,
        patch("src.core.tools.creative_formats.get_audit_logger") as mock_audit,
    ):
        mock_reg = MagicMock()

        async def mock_list_formats(**kwargs):
            return list(formats)  # Copy to avoid mutation

        async def mock_list_formats_with_errors(**kwargs):
            return FormatFetchResult(formats=list(formats), errors=[])

        mock_reg.list_all_formats = mock_list_formats
        mock_reg.list_all_formats_with_errors = mock_list_formats_with_errors
        mock_registry.return_value = mock_reg

        # Mock audit logger to avoid any side effects
        mock_audit.return_value = MagicMock()

        response = _list_creative_formats_impl(req, identity)
        return response.formats


# ---------------------------------------------------------------------------
# HIGH_RISK: Sort order — T-UC-005-inv10
# ---------------------------------------------------------------------------


class TestSortOrderByName:
    """T-UC-005-inv10: Results sorted by name.

    Behavioral contract at creative_formats.py:337. Refactoring during
    migration could silently reorder results. In adcp 3.12, type was removed
    from Format, so sorting is now by name only.
    """

    def test_sort_order_by_name(self):
        """Formats must be sorted alphabetically by name."""
        formats = [
            _make_format("v_zebra", "Zebra Ad"),
            _make_format("d_alpha", "Alpha Banner"),
            _make_format("v_alpha", "Alpha Video"),
            _make_format("d_zebra", "Zebra Banner"),
        ]

        result = _call_impl(formats)

        names = [f.name for f in result]
        assert names == [
            "Alpha Banner",
            "Alpha Video",
            "Zebra Ad",
            "Zebra Banner",
        ], f"Expected alphabetical ordering but got {names}"

    def test_sort_order_across_many_formats(self):
        """Sort order holds across many formats."""
        formats = [
            _make_format("n1", "Native B"),
            _make_format("d1", "Display A"),
            _make_format("v1", "Video C"),
            _make_format("n2", "Native A"),
            _make_format("d2", "Display B"),
        ]

        result = _call_impl(formats)

        names = [f.name for f in result]
        assert names == [
            "Display A",
            "Display B",
            "Native A",
            "Native B",
            "Video C",
        ], f"Expected alphabetical ordering but got {names}"

    def test_sort_preserves_after_filtering(self):
        """Sort order is maintained even after filters reduce the set."""
        formats = [
            _make_format("v2", "Zebra Video"),
            _make_format("v1", "Alpha Video"),
            _make_format("d1", "Display Ad"),
        ]

        result = _call_impl(formats)

        names = [f.name for f in result]
        assert names == ["Alpha Video", "Display Ad", "Zebra Video"], (
            f"Results should be sorted alphabetically: {names}"
        )


# ---------------------------------------------------------------------------
# MEDIUM_RISK: Type filter empty result — T-UC-005-inv2-violated
# ---------------------------------------------------------------------------


class TestTypeFilterRemovedInAdcp312:
    """T-UC-005-inv2-violated: Type filter removed in adcp 3.12."""

    def test_type_filter_rejected(self):
        """type= parameter is no longer accepted on ListCreativeFormatsRequest."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="type"):
            ListCreativeFormatsRequest(type="audio")

    def test_empty_catalog_returns_empty(self):
        """Empty catalog returns empty list."""
        result = _call_impl([])
        assert result == []


# ---------------------------------------------------------------------------
# MEDIUM_RISK: Group asset filtering — T-UC-005-inv4-group
# ---------------------------------------------------------------------------


class TestAssetTypesFilterChecksGroupAssets:
    """T-UC-005-inv4-group: asset_types filter checks nested group assets.

    Code at creative_formats.py:245-264 iterates get_format_assets() and
    checks both individual asset_type AND nested assets within groups.
    """

    def test_asset_types_filter_finds_type_in_group_assets(self):
        """Format with group assets containing requested type should be included."""
        # adcp 7: repeatable_group uses Assets24, nested items use the Assets25 union
        # (Assets26 = image, Assets29 = text)
        from adcp.types.generated_poc.core.format import Assets24, Assets26, Assets29

        group_asset = Assets24(
            item_type="repeatable_group",
            asset_group_id="product_group",
            required=True,
            min_count=1,
            max_count=5,
            assets=[
                Assets26(
                    asset_id="product_image",
                    required=True,
                ),
                Assets29(
                    asset_id="product_title",
                    required=True,
                ),
            ],
        )

        format_with_group = Format(
            format_id=FormatId(agent_url=DEFAULT_AGENT_URL, id="native_carousel"),
            name="Native Carousel",
            is_standard=True,
            assets=[group_asset],
        )

        # Filter for "image" — should match via nested group asset
        req = ListCreativeFormatsRequest(asset_types=["image"])
        result = _call_impl([format_with_group], req)

        assert len(result) == 1, "Group asset with image should match image filter"
        assert result[0].name == "Native Carousel"

    def test_asset_types_filter_excludes_group_without_match(self):
        """Format with group assets NOT containing requested type should be excluded."""
        # adcp 7: repeatable_group uses Assets24, nested text items use Assets29
        from adcp.types.generated_poc.core.format import Assets24, Assets29

        group_asset = Assets24(
            item_type="repeatable_group",
            asset_group_id="text_group",
            required=True,
            min_count=1,
            max_count=3,
            assets=[
                Assets29(
                    asset_id="headline",
                    required=True,
                ),
            ],
        )

        format_with_text_group = Format(
            format_id=FormatId(agent_url=DEFAULT_AGENT_URL, id="text_only"),
            name="Text Only Native",
            is_standard=True,
            assets=[group_asset],
        )

        # Filter for "video" — should NOT match since group only has text
        req = ListCreativeFormatsRequest(asset_types=["video"])
        result = _call_impl([format_with_text_group], req)

        assert result == [], "Group asset without video should not match video filter"

    def test_asset_types_filter_mixed_individual_and_group(self):
        """Format with both individual and group assets: filter checks both."""
        # adcp 7: Assets5 (= Assets10) = individual video, Assets24 = repeatable_group
        # Assets24 nested assets use the Assets25 union (image = Assets26)
        from adcp.types.generated_poc.core.format import Assets24, Assets26

        individual_asset = Assets5(
            asset_id="hero_video",
            required=True,
        )
        group_asset = Assets24(
            item_type="repeatable_group",
            asset_group_id="product_group",
            required=False,
            min_count=0,
            max_count=5,
            assets=[
                Assets26(
                    asset_id="product_image",
                    required=True,
                ),
            ],
        )

        mixed_format = Format(
            format_id=FormatId(agent_url=DEFAULT_AGENT_URL, id="mixed_format"),
            name="Mixed Format",
            is_standard=True,
            assets=[individual_asset, group_asset],
        )

        # Filter for "image" — should match via group nested asset
        req = ListCreativeFormatsRequest(asset_types=["image"])
        result = _call_impl([mixed_format], req)
        assert len(result) == 1, "Mixed format should match image via group asset"

        # Filter for "video" — should match via individual asset
        req = ListCreativeFormatsRequest(asset_types=["video"])
        result = _call_impl([mixed_format], req)
        assert len(result) == 1, "Mixed format should match video via individual asset"

        # Filter for "html" — should NOT match
        req = ListCreativeFormatsRequest(asset_types=["html"])
        result = _call_impl([mixed_format], req)
        assert result == [], "Mixed format should not match html (not present)"


# ---------------------------------------------------------------------------
# LOW_RISK: Partition/boundary completeness
# ---------------------------------------------------------------------------


class TestPartitionTypeFilterRemovedInAdcp312:
    """T-UC-005-partition-type-filter: type filter removed in adcp 3.12."""

    def test_type_filter_no_longer_accepted(self):
        """type= parameter is no longer accepted on ListCreativeFormatsRequest in adcp 3.12."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="type"):
            ListCreativeFormatsRequest(type="native")


class TestPartitionFormatIdsNoMatch:
    """T-UC-005-partition-format-ids: no match row."""

    def test_format_ids_no_match_returns_empty(self):
        """When format_ids contains only non-existent IDs, returns empty list."""
        formats = [
            _make_format("display_300x250", "Display 300x250"),
            _make_format("display_728x90", "Display 728x90"),
            _make_format("video_16x9", "Video 16:9"),
        ]

        non_existent_ids = [
            FormatId(agent_url=DEFAULT_AGENT_URL, id="nonexistent_format"),
        ]
        req = ListCreativeFormatsRequest(format_ids=non_existent_ids)
        result = _call_impl(formats, req)

        assert result == [], f"Non-matching format_ids should return empty, got {len(result)}"


class TestBoundaryDimensionExactMax:
    """T-UC-005-boundary-dimension: exact max boundary (inclusive)."""

    def test_boundary_dimension_exact_max_width(self):
        """Format with width=300 included when max_width=300 (inclusive boundary)."""
        formats = [
            _make_format(
                "rect",
                "Medium Rectangle",
                renders=[Renders(role="primary", dimensions=Dimensions(width=300, height=250))],
            ),
        ]

        req = ListCreativeFormatsRequest(max_width=300)
        result = _call_impl(formats, req)

        assert len(result) == 1, "Width=300 should be included by max_width=300"
        assert result[0].name == "Medium Rectangle"

    def test_boundary_dimension_off_by_one_max_width(self):
        """Format with width=301 excluded when max_width=300."""
        formats = [
            _make_format(
                "wide",
                "Slightly Wide",
                renders=[Renders(role="primary", dimensions=Dimensions(width=301, height=250))],
            ),
        ]

        req = ListCreativeFormatsRequest(max_width=300)
        result = _call_impl(formats, req)

        assert result == [], "Width=301 should be excluded by max_width=300"

    def test_boundary_dimension_exact_min_width(self):
        """Format with width=300 included when min_width=300 (inclusive boundary)."""
        formats = [
            _make_format(
                "rect",
                "Medium Rectangle",
                renders=[Renders(role="primary", dimensions=Dimensions(width=300, height=250))],
            ),
        ]

        req = ListCreativeFormatsRequest(min_width=300)
        result = _call_impl(formats, req)

        assert len(result) == 1, "Width=300 should be included by min_width=300"

    def test_boundary_dimension_off_by_one_min_width(self):
        """Format with width=299 excluded when min_width=300."""
        formats = [
            _make_format(
                "narrow",
                "Slightly Narrow",
                renders=[Renders(role="primary", dimensions=Dimensions(width=299, height=250))],
            ),
        ]

        req = ListCreativeFormatsRequest(min_width=300)
        result = _call_impl(formats, req)

        assert result == [], "Width=299 should be excluded by min_width=300"


class TestBoundaryNameSearchEmptyString:
    """T-UC-005-boundary-name-search: empty string boundary.

    Code at creative_formats.py:272 uses 'if req.name_search:' — Python
    falsy check — so empty string is treated as no filter (all returned).
    Per architect review, assert should be 'returns all formats'.
    """

    def test_empty_string_name_search_returns_all(self):
        """Empty string name_search is treated as no filter (all formats returned)."""
        formats = [
            _make_format("d1", "Alpha Display"),
            _make_format("v1", "Beta Video"),
            _make_format("n1", "Gamma Native"),
        ]

        req = ListCreativeFormatsRequest(name_search="")
        result = _call_impl(formats, req)

        assert len(result) == 3, f"Empty name_search should return all formats, got {len(result)}"


# ---------------------------------------------------------------------------
# Enhancement: asset_types exclusion — T-UC-005-inv4-violated
# ---------------------------------------------------------------------------


class TestAssetTypesFilterExclusion:
    """T-UC-005-inv4-violated: format with non-matching asset types excluded."""

    def test_format_with_non_matching_assets_excluded(self):
        """Format with assets that do not match any requested type is excluded."""
        # adcp 3.6.0: use typed asset classes - Assets (image), Assets9 (html)
        from adcp.types.generated_poc.core.format import Assets14 as Assets9

        formats = [
            _make_format(
                "image_banner",
                "Image Banner",
                assets=[
                    Assets(
                        asset_id="main",
                        required=True,
                    ),
                ],
            ),
            _make_format(
                "html_widget",
                "HTML Widget",
                assets=[
                    Assets9(
                        asset_id="code",
                        required=True,
                    ),
                ],
            ),
        ]

        # Filter for video — neither format has video assets
        req = ListCreativeFormatsRequest(asset_types=["video"])
        result = _call_impl(formats, req)

        assert result == [], "Formats with only image/html assets should be excluded by video filter"

    def test_format_with_assets_of_wrong_type_excluded_while_match_kept(self):
        """Only formats with at least one matching asset type are kept."""
        # adcp 3.6.0: Assets (image), Assets5 (video)
        formats = [
            _make_format(
                "image_only",
                "Image Only",
                assets=[
                    Assets(
                        asset_id="photo",
                        required=True,
                    ),
                ],
            ),
            _make_format(
                "video_format",
                "Video Format",
                assets=[
                    Assets5(
                        asset_id="clip",
                        required=True,
                    ),
                ],
            ),
        ]

        req = ListCreativeFormatsRequest(asset_types=["video"])
        result = _call_impl(formats, req)

        assert len(result) == 1
        assert result[0].name == "Video Format"


class TestBroadstreetTemplateAssetParsing:
    """Regression: Broadstreet formats must parse with real, typed assets.

    Sales Agent 2.0 replaced the per-template ``_infer_asset_type()`` /
    ``_make_asset()`` builders with canonical reference-agent formats
    (``src/adapters/broadstreet/formats.py:broadstreet_creative_format_models``).
    Each Broadstreet template type maps onto one canonical format, and every
    asset those formats declare must carry a spec ``asset_type`` literal. The
    original bug constructed ``Assets(asset_type=AssetContentType(...))`` against
    a ``Literal['image']`` field, which failed validation and left formats with
    no assets.
    """

    @staticmethod
    def _leaf_assets(fmt: Format) -> list:
        """Flatten a format's asset declarations (expanding repeatable groups)."""
        from adcp.utils.format_assets import get_format_assets

        leaves = []
        for entry in get_format_assets(fmt):
            nested = getattr(entry, "assets", None)
            leaves.extend(list(nested) if nested else [entry])
        return leaves

    def test_all_broadstreet_templates_produce_formats_with_assets(self):
        """Every Broadstreet template with a canonical format must produce a Format with non-empty, typed assets."""
        from src.adapters.broadstreet.config_schema import BROADSTREET_TEMPLATES
        from src.adapters.broadstreet.formats import (
            BROADSTREET_TEMPLATE_CANONICAL_FORMAT_IDS,
            broadstreet_creative_format_models,
            broadstreet_template_canonical_format_id,
        )

        formats_by_id = {fmt.format_id.id: fmt for fmt in broadstreet_creative_format_models()}
        assert formats_by_id, "Broadstreet must advertise at least one canonical format"

        # Every advertised format declares assets, each carrying a spec asset_type literal
        for format_id, fmt in formats_by_id.items():
            assert fmt.assets, f"Format {format_id} must have non-empty assets list"
            leaves = self._leaf_assets(fmt)
            assert leaves, f"Format {format_id} must declare at least one leaf asset"
            for leaf in leaves:
                asset_type = getattr(leaf, "asset_type", None)
                assert isinstance(asset_type, str) and asset_type, (
                    f"Format {format_id} asset {getattr(leaf, 'asset_id', None)!r} must carry an asset_type literal"
                )

        # The template -> canonical mapping only names real templates, and every mapped
        # template resolves to one of the advertised formats.
        assert set(BROADSTREET_TEMPLATE_CANONICAL_FORMAT_IDS) <= set(BROADSTREET_TEMPLATES)
        mapped = {tid: broadstreet_template_canonical_format_id(tid) for tid in BROADSTREET_TEMPLATES}
        assert any(canonical_id is not None for canonical_id in mapped.values())
        for tid, canonical_id in mapped.items():
            if canonical_id is None:
                continue  # template has no canonical reference-agent equivalent; not advertised
            assert canonical_id in formats_by_id, f"Template {tid} maps to unadvertised format {canonical_id}"

    def test_asset_type_literals_match_inferred_type(self):
        """Each declared asset must have an asset_type literal matching its asset role."""
        from src.adapters.broadstreet.formats import broadstreet_creative_format_models

        formats_by_id = {fmt.format_id.id: fmt for fmt in broadstreet_creative_format_models()}

        for format_id, asset_id, expected_type in [
            ("display_image", "banner_image", "image"),
            ("display_image", "click_url", "url"),
            ("display_html", "html_creative", "html"),
            ("display_js", "js_creative", "javascript"),
            ("native_standard", "title", "text"),
            ("native_standard", "main_image", "image"),
        ]:
            assert format_id in formats_by_id, f"Broadstreet must advertise {format_id}"
            by_asset_id = {getattr(a, "asset_id", None): a for a in self._leaf_assets(formats_by_id[format_id])}
            assert asset_id in by_asset_id, f"{format_id} must declare asset {asset_id!r}"
            asset = by_asset_id[asset_id]
            assert asset.asset_type == expected_type, (
                f"{format_id}/{asset_id}: asset_type should be '{expected_type}', got '{asset.asset_type}'"
            )


class TestMCPWrapperStringCoercion:
    """Regression: the transport boundary must coerce raw string enum params.

    The legacy MCP wrapper did ``type.value`` / ``at.value`` and crashed with
    AttributeError when handed raw strings. In Sales Agent 2.0 the boundary is
    ``core/platforms/_delegate.py:_delegate_list_creative_formats`` (dispatched
    by the adcp SDK server for both MCP and A2A), which coerces the wire dict
    into ``ListCreativeFormatsRequest`` — enum fields validated by pydantic —
    before calling ``_list_creative_formats_impl``.
    """

    @staticmethod
    def _dispatch(req) -> MagicMock:
        """Run the delegate on a wire dict with identity + impl stubbed; return the impl mock."""
        from core.platforms import _delegate
        from src.core.schemas import ListCreativeFormatsResponse

        identity = PrincipalFactory.make_identity(
            principal_id=None,
            tenant_id=MOCK_TENANT["tenant_id"],
            tenant=MOCK_TENANT,
        )
        with (
            patch.object(_delegate, "_build_identity", return_value=identity),
            patch.object(
                _delegate, "_list_creative_formats_impl", return_value=ListCreativeFormatsResponse(formats=[])
            ) as mock_impl,
        ):
            asyncio.run(_delegate._delegate_list_creative_formats(req, object()))
        return mock_impl

    def test_mcp_wrapper_handles_string_type(self):
        """Boundary must not crash on a bare wire dict and must hand _impl a request model."""
        # A raw wire dict (no FastMCP enum coercion) must not raise AttributeError
        mock_impl = self._dispatch({})

        mock_impl.assert_called_once()
        req_model, _identity = mock_impl.call_args.args
        assert isinstance(req_model, ListCreativeFormatsRequest)
        assert req_model.asset_types is None

    def test_mcp_wrapper_handles_string_asset_types(self):
        """Boundary must coerce raw string asset_types into AssetContentType enums before _impl."""
        from adcp.types.generated_poc.enums.asset_content_type import AssetContentType

        mock_impl = self._dispatch({"asset_types": ["image", "video"]})

        mock_impl.assert_called_once()
        req_model, _identity = mock_impl.call_args.args
        assert isinstance(req_model, ListCreativeFormatsRequest)
        assert req_model.asset_types == [AssetContentType.image, AssetContentType.video]


# ---------------------------------------------------------------------------
# Extension C: Error propagation in format discovery
# Decision: docs/design/error-propagation-in-format-discovery.md
# ---------------------------------------------------------------------------


def _call_impl_raw(
    formats: list[Format],
    registry_side_effect: Exception | None = None,
    errors: list | None = None,
):
    """Call _list_creative_formats_impl and return the full response (not just formats).

    Args:
        formats: Formats returned by healthy agents.
        registry_side_effect: If set, get_creative_agent_registry() raises this.
        errors: List of AdCP Error objects to include as per-agent failures.
    """
    from src.core.creative_agent_registry import FormatFetchResult
    from src.core.tools.creative_formats import _list_creative_formats_impl

    req = ListCreativeFormatsRequest()
    identity = PrincipalFactory.make_identity(
        principal_id=None,
        tenant_id=MOCK_TENANT["tenant_id"],
        tenant=MOCK_TENANT,
    )

    if registry_side_effect:
        with patch(
            "src.core.creative_agent_registry.get_creative_agent_registry",
            side_effect=registry_side_effect,
        ):
            with patch("src.core.tools.creative_formats.get_audit_logger") as mock_audit:
                mock_audit.return_value = MagicMock()
                return _list_creative_formats_impl(req, identity)

    with (
        patch("src.core.creative_agent_registry.get_creative_agent_registry") as mock_registry,
        patch("src.core.tools.creative_formats.get_audit_logger") as mock_audit,
    ):
        mock_reg = MagicMock()
        fetch_result = FormatFetchResult(formats=list(formats), errors=errors or [])

        async def mock_list_formats_with_errors(**kwargs):
            return fetch_result

        async def mock_list_formats(**kwargs):
            return list(formats)

        mock_reg.list_all_formats_with_errors = mock_list_formats_with_errors
        mock_reg.list_all_formats = mock_list_formats
        mock_reg._get_tenant_agents = MagicMock(return_value=[])
        mock_registry.return_value = mock_reg
        mock_audit.return_value = MagicMock()

        return _list_creative_formats_impl(req, identity)


class TestPartialAgentFailureReturnsFormatsAndErrors:
    """UC-005-EXT-C-01: Partial agent failure returns formats from healthy agents plus errors.

    Decision: docs/design/error-propagation-in-format-discovery.md (FD-ERR-01)
    """

    def test_partial_agent_failure_returns_formats_and_errors(self):
        """Covers: UC-005-EXT-C-01

        Given two registered creative agents, one healthy and one unreachable,
        when list_creative_formats is called,
        then the response contains formats from the healthy agent
        and an errors[] entry for the failed agent.
        """
        from adcp.types.generated_poc.core.error import Error as AdCPResponseError

        healthy_formats = [
            _make_format("display_300x250", "Display 300x250"),
        ]
        agent_errors = [
            AdCPResponseError(
                code="AGENT_UNREACHABLE",
                message="Creative agent at https://failing-agent.example.com is unreachable: Connection refused",
            ),
        ]
        response = _call_impl_raw(healthy_formats, errors=agent_errors)

        # Formats from healthy agent should be present
        assert len(response.formats) >= 1

        # Errors should report the failed agent
        assert response.errors is not None, "Response must include errors[] for failed agents, not silently drop them"
        assert len(response.errors) >= 1
        # Each error must have code and message per AdCP error.json
        for err in response.errors:
            assert err.code is not None
            assert err.message is not None


class TestAllAgentsFailReturnsEmptyFormatsAndErrors:
    """UC-005-EXT-C-02: All agents fail returns empty formats plus errors.

    Decision: docs/design/error-propagation-in-format-discovery.md (FD-ERR-02)
    """

    def test_all_agents_fail_returns_empty_formats_and_errors(self):
        """Covers: UC-005-EXT-C-02

        Given all registered creative agents are unreachable,
        when list_creative_formats is called,
        then the response contains an empty formats array
        and errors[] with one entry per failed agent.
        """
        from adcp.types.generated_poc.core.error import Error as AdCPResponseError

        # Simulate all agents failing — registry returns no formats but reports errors
        agent_errors = [
            AdCPResponseError(
                code="AGENT_UNREACHABLE",
                message="Creative agent at https://agent-1.example.com is unreachable: Connection refused",
            ),
            AdCPResponseError(
                code="AGENT_UNREACHABLE",
                message="Creative agent at https://agent-2.example.com is unreachable: Timeout",
            ),
        ]
        response = _call_impl_raw(formats=[], errors=agent_errors)

        assert response.formats == []
        assert response.errors is not None, (
            "Total agent failure must return errors[], not bare empty formats. "
            "An empty formats[] without errors[] means 'no formats configured', "
            "not 'agents are down'."
        )
        assert len(response.errors) >= 1
        for err in response.errors:
            assert err.code is not None
            assert err.message is not None


class TestRegistryCreationFailureReturnsErrors:
    """UC-005-EXT-C-03: Registry creation failure returns empty formats plus errors.

    Decision: docs/design/error-propagation-in-format-discovery.md (FD-ERR-03)
    """

    def test_registry_creation_failure_returns_errors(self):
        """Covers: UC-005-EXT-C-03

        Given the creative agent registry cannot be initialized,
        when list_creative_formats is called,
        then the response contains an empty formats array
        and errors[] describing the infrastructure failure.
        """
        response = _call_impl_raw(
            formats=[],
            registry_side_effect=RuntimeError("Cannot connect to agent registry"),
        )

        assert response.formats == []
        assert response.errors is not None, "Infrastructure failure must be reported in errors[], not swallowed"
        assert len(response.errors) >= 1
        assert any("registry" in err.message.lower() for err in response.errors)


class TestErrorEntriesFollowAdCPSchema:
    """UC-005-EXT-C-04: Error entries follow AdCP error.json schema.

    Decision: docs/design/error-propagation-in-format-discovery.md (FD-ERR-04)
    """

    def test_error_entries_have_code_and_message(self):
        """Covers: UC-005-EXT-C-04

        Given a list_creative_formats response with errors,
        when the Buyer inspects the errors[] array,
        then each error has code (string) and message (string) at minimum,
        conforming to error.json schema.
        """
        from adcp.types.generated_poc.core.error import Error

        response = _call_impl_raw(
            formats=[],
            registry_side_effect=RuntimeError("Test infrastructure failure"),
        )

        assert response.errors is not None
        for err in response.errors:
            assert isinstance(err, Error), f"Error must be an AdCP Error instance, got {type(err)}"
            assert isinstance(err.code, str) and len(err.code) > 0
            assert isinstance(err.message, str) and len(err.message) > 0


class TestSuccessfulDiscoveryHasNoErrors:
    """UC-005-EXT-C-05: Successful discovery has no errors.

    Decision: docs/design/error-propagation-in-format-discovery.md (FD-ERR-05)
    """

    def test_successful_discovery_has_no_errors(self):
        """Covers: UC-005-EXT-C-05

        Given all registered creative agents are healthy,
        when list_creative_formats is called,
        then the response contains formats from all agents
        and the errors field is absent or an empty array.
        """
        formats = [
            _make_format("display_300x250", "Display 300x250"),
            _make_format("video_16x9", "Video 16:9"),
        ]

        response = _call_impl_raw(formats)

        assert len(response.formats) == 2
        assert response.errors is None or response.errors == [], (
            f"Successful discovery must have no errors, got {response.errors}"
        )


class TestAgentReferralFailureLogsWarning:
    """Agent referral building errors must be logged, not silently swallowed.

    CLAUDE.md: No Quiet Failures pattern. If _get_tenant_agents raises,
    creative_agents_list should remain None (non-critical) but a warning
    must be logged so operators can diagnose the issue.
    """

    def test_referral_error_logs_warning(self, caplog):
        """When _get_tenant_agents raises, a warning is logged (not silent pass)."""
        import logging

        from src.core.creative_agent_registry import FormatFetchResult
        from src.core.tools.creative_formats import _list_creative_formats_impl

        formats = [_make_format("display_300x250", "Display 300x250")]
        mock_reg = MagicMock()
        mock_reg._get_tenant_agents = MagicMock(side_effect=RuntimeError("Agent DB connection refused"))
        fetch_result = FormatFetchResult(formats=list(formats), errors=[])

        async def mock_list_formats_with_errors(**kwargs):
            return fetch_result

        async def mock_list_formats(**kwargs):
            return list(formats)

        mock_reg.list_all_formats_with_errors = mock_list_formats_with_errors
        mock_reg.list_all_formats = mock_list_formats

        identity = PrincipalFactory.make_identity(
            principal_id=None,
            tenant_id=MOCK_TENANT["tenant_id"],
            tenant=MOCK_TENANT,
        )

        with (
            patch(
                "src.core.creative_agent_registry.get_creative_agent_registry",
                return_value=mock_reg,
            ),
            patch(
                "src.core.tools.creative_formats.get_audit_logger",
                return_value=MagicMock(),
            ),
            caplog.at_level(logging.WARNING, logger="src.core.tools.creative_formats"),
        ):
            _list_creative_formats_impl(
                req=ListCreativeFormatsRequest(),
                identity=identity,
            )

        # After fix: should log a warning about the referral failure
        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("referral" in msg.lower() or "agent" in msg.lower() for msg in warning_msgs), (
            f"Expected a warning about agent referral failure, got: {warning_msgs}"
        )
