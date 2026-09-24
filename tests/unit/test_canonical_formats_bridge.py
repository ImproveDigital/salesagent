"""adcp 7 canonical creative identity bridge (core/platforms/_canonical_formats.py).

The SDK dispatcher hands platforms canonical requests and expects canonical
results; salesagent impls speak the legacy named-format shape. These tests
pin both translation directions plus the two SDK hooks (converter / resolver).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from adcp.canonical_formats import (
    CanonicalFormatLegacyResolutionContext,
    LegacyFormatConversionContext,
    migrated_format_option_id,
    normalize_legacy_creative_request,
    project_canonical_response_to_legacy,
)
from adcp.types.legacy import LegacyFormatId

from core.platforms import _canonical_formats as bridge
from src.core.exceptions import AdCPInvalidRequestError

AAO = "https://creative.adcontextprotocol.org"


def _ref(fmt_id: str, agent_url: str = AAO, **params):
    return {"agent_url": agent_url, "id": fmt_id, **params}


@pytest.fixture(autouse=True)
def _fresh_index():
    bridge.reset_for_tests()
    yield
    bridge.reset_for_tests()


class TestDeclarationProjection:
    def test_catalog_format_projects_via_sdk_registry(self):
        decl = bridge.declaration_for_legacy_ref(_ref("display_300x250"), product_id="p1")
        assert decl is not None
        assert decl.format_kind.value == "image"
        assert decl.format_option_id == migrated_format_option_id(LegacyFormatId(**_ref("display_300x250")))

    def test_standard_format_outside_sdk_catalog_uses_our_converter(self):
        # audio_vast is in salesagent's standard catalog but not the SDK's bundled AAO index.
        decl = bridge.declaration_for_legacy_ref(_ref("audio_vast"), product_id="p1")
        assert decl is not None
        assert decl.format_kind.value == "audio_daast"

    def test_parameterised_tuple_carries_dimensions(self):
        decl = bridge.declaration_for_legacy_ref(_ref("video_standard", width=1920, height=1080))
        assert decl is not None
        assert decl.params == {"width": 1920, "height": 1080}

    def test_unknown_format_is_not_projected(self):
        assert bridge.declaration_for_legacy_ref(_ref("no_such_format_xyz")) is None

    def test_converter_hook_returns_none_for_unknown_format(self):
        ctx = LegacyFormatConversionContext(LegacyFormatId(**_ref("no_such_format_xyz")), "p1", "f")
        assert bridge.legacy_format_converter(ctx) is None

    def test_converter_hook_infers_conventional_id_from_unknown_owner(self):
        ref = LegacyFormatId(
            agent_url="https://creatives.adcontextprotocol.org", id="display_static", width=300, height=250
        )
        body = bridge.legacy_format_converter(LegacyFormatConversionContext(ref, "p1", "f"))
        assert body == {"format_kind": "image", "params": {"width": 300, "height": 250}}


class TestResolver:
    def test_round_trip_in_process(self):
        decl = bridge.declaration_for_legacy_ref(_ref("audio_vast"))
        refs = bridge.canonical_format_legacy_resolver(CanonicalFormatLegacyResolutionContext(declaration=decl))
        assert [r.model_dump(exclude_none=True) for r in refs] == [_ref("audio_vast")]

    def test_cold_resolution_rebuilds_from_catalog(self):
        option_id = migrated_format_option_id(LegacyFormatId(**_ref("audio_vast")))
        bridge.reset_for_tests()
        resolved = bridge.resolve_option_id(option_id, tenant_id=None)
        assert resolved is not None and resolved.id == "audio_vast"

    def test_agent_url_trailing_slash_is_irrelevant(self):
        with_slash = migrated_format_option_id(LegacyFormatId(**_ref("display_300x250", agent_url=AAO + "/")))
        without = migrated_format_option_id(LegacyFormatId(**_ref("display_300x250")))
        assert with_slash != without  # the SDK hashes the raw string ...
        bridge.declaration_for_legacy_ref(_ref("display_300x250"))
        assert bridge.resolve_option_id(with_slash) is not None  # ... we index both spellings
        assert bridge.resolve_option_id(without) is not None

    def test_unknown_option_id_resolves_to_none(self):
        assert bridge.resolve_option_id("migrated_" + "0" * 32, tenant_id=None) is None


class TestInboundTranslation:
    def test_get_products_filters_from_sdk_normalised_legacy_request(self):
        # What the dispatcher produces for a legacy buyer's ``filters.format_ids``.
        params = normalize_legacy_creative_request(
            {"buying_mode": "brief", "brief": "x", "filters": {"format_ids": [_ref("display_300x250")]}}
        )
        body = bridge.legacy_request_payload("get_products", params)
        assert body["filters"] == {"format_ids": [_ref("display_300x250")]}

    def test_get_products_filters_from_canonical_buyer(self):
        option_id = bridge.declaration_for_legacy_ref(_ref("audio_vast")).format_option_id
        body = bridge.legacy_request_payload(
            "get_products",
            {
                "buying_mode": "brief",
                "brief": "x",
                "fields": ["format_options", "product_id"],
                "filters": {
                    "format_options": [{"format_option_id": option_id, "format_kind": "audio_daast", "params": {}}]
                },
            },
        )
        assert body["filters"]["format_ids"] == [_ref("audio_vast")]
        assert body["fields"] == ["format_ids", "product_id"]

    def test_sync_creatives_canonical_creative_gets_format_id(self):
        option_id = bridge.declaration_for_legacy_ref(_ref("display_300x250")).format_option_id
        creative = {
            "creative_id": "c1",
            "name": "Banner",
            "format_kind": "image",
            "format_option_ref": {
                "scope": "publisher",
                "publisher_domain": "seller.example",
                "format_option_id": option_id,
            },
            "assets": {},
        }
        body = bridge.legacy_request_payload("sync_creatives", {"creatives": [creative]})
        out = body["creatives"][0]
        assert out["format_id"] == _ref("display_300x250")
        assert "format_kind" not in out and "format_option_ref" not in out

    def test_sync_creatives_legacy_creative_passes_through(self):
        creative = {"creative_id": "c1", "name": "Banner", "format_id": _ref("display_300x250"), "assets": {}}
        assert bridge.legacy_request_payload("sync_creatives", {"creatives": [creative]})["creatives"][0] == creative

    def test_sync_creatives_unknown_option_is_invalid_request(self):
        creative = {
            "creative_id": "c1",
            "format_kind": "image",
            "format_option_ref": {"scope": "product", "format_option_id": "migrated_" + "f" * 32},
        }
        with pytest.raises(AdCPInvalidRequestError, match="unknown format option"):
            bridge.legacy_request_payload("sync_creatives", {"creatives": [creative]})

    def test_sync_creatives_format_kind_without_ref_is_invalid_request(self):
        with pytest.raises(AdCPInvalidRequestError, match="without a format_option_ref"):
            bridge.legacy_request_payload(
                "sync_creatives", {"creatives": [{"creative_id": "c1", "format_kind": "image"}]}
            )

    def test_create_media_buy_packages_option_refs_become_format_ids(self):
        option_id = bridge.declaration_for_legacy_ref(_ref("display_300x250"), product_id="p1").format_option_id
        body = bridge.legacy_request_payload(
            "create_media_buy",
            {
                "packages": [
                    {
                        "buyer_ref": "b",
                        "product_id": "p1",
                        "format_option_refs": [{"scope": "product", "format_option_id": option_id}],
                    }
                ]
            },
        )
        assert body["packages"][0]["format_ids"] == [_ref("display_300x250")]
        assert "format_option_refs" not in body["packages"][0]

    def test_create_media_buy_inline_package_creatives_get_format_id(self):
        option_id = bridge.declaration_for_legacy_ref(_ref("display_300x250"), product_id="p1").format_option_id
        body = bridge.legacy_request_payload(
            "create_media_buy",
            {
                "packages": [
                    {
                        "buyer_ref": "b",
                        "product_id": "p1",
                        "format_option_refs": [{"scope": "product", "format_option_id": option_id}],
                        "creatives": [
                            {
                                "creative_id": "c1",
                                "name": "Banner",
                                "format_kind": "image",
                                "format_option_ref": {"scope": "product", "format_option_id": option_id},
                                "assets": {},
                            },
                            {"creative_id": "c2", "name": "Legacy", "format_id": _ref("display_300x250"), "assets": {}},
                        ],
                    }
                ]
            },
        )
        creatives = body["packages"][0]["creatives"]
        assert creatives[0]["format_id"] == _ref("display_300x250")
        assert "format_kind" not in creatives[0] and "format_option_ref" not in creatives[0]
        assert creatives[1]["format_id"] == _ref("display_300x250")

    def test_list_creatives_filters_from_sdk_normalised_legacy_request(self):
        params = normalize_legacy_creative_request({"filters": {"format_ids": [_ref("display_300x250")]}})
        body = bridge.legacy_request_payload("list_creatives", params)
        assert body["filters"] == {"format_ids": [_ref("display_300x250")]}

    def test_other_tools_untouched(self):
        payload = {"media_buy_ids": ["mb_1"]}
        assert bridge.legacy_request_payload("get_media_buys", payload) == payload


class TestOutboundCanonicalisation:
    def test_products_emit_canonical_format_options_only(self):
        wire = {"products": [{"product_id": "p1", "format_ids": [_ref("display_300x250"), _ref("audio_vast")]}]}
        out = bridge.canonicalize_wire_response("get_products", wire)
        product = out["products"][0]
        assert "format_ids" not in product
        assert [o["format_kind"] for o in product["format_options"]] == ["image", "audio_daast"]
        assert all(o["format_option_id"].startswith("migrated_") for o in product["format_options"])
        assert all("v1_format_ref" not in o for o in product["format_options"])

    def test_product_with_unprojectable_format_keeps_legacy_ids(self):
        wire = {"products": [{"product_id": "p1", "format_ids": [_ref("display_300x250"), _ref("no_such_format_xyz")]}]}
        out = bridge.canonicalize_wire_response("get_products", wire)
        assert out["products"][0]["format_ids"] == wire["products"][0]["format_ids"]
        assert "format_options" not in out["products"][0]

    def test_packages_emit_option_refs(self):
        wire = {
            "packages": [
                {
                    "package_id": "pk",
                    "product_id": "p1",
                    "format_ids": [_ref("display_300x250")],
                    "format_ids_to_provide": [],
                }
            ]
        }
        pkg = bridge.canonicalize_wire_response("create_media_buy", wire)["packages"][0]
        assert pkg["format_option_refs"] == [
            {
                "scope": "product",
                "format_option_id": migrated_format_option_id(LegacyFormatId(**_ref("display_300x250"))),
            }
        ]
        assert "format_ids" not in pkg and "format_ids_to_provide" not in pkg

    def test_media_buys_packages_emit_option_refs(self):
        wire = {
            "media_buys": [
                {"media_buy_id": "mb", "packages": [{"package_id": "pk", "format_ids": [_ref("display_300x250")]}]}
            ]
        }
        pkg = bridge.canonicalize_wire_response("get_media_buys", wire)["media_buys"][0]["packages"][0]
        assert pkg["format_option_refs"][0]["scope"] == "product"

    def test_listed_creatives_emit_kind_and_publisher_ref(self):
        wire = {"creatives": [{"creative_id": "c1", "format_id": _ref("display_300x250")}]}
        creative = bridge.canonicalize_wire_response("list_creatives", wire)["creatives"][0]
        assert creative["format_kind"] == "image"
        assert creative["format_option_ref"]["scope"] == "publisher"
        assert creative["format_option_ref"]["publisher_domain"]
        assert "format_id" not in creative

    def test_sdk_projects_our_canonical_products_back_to_legacy(self):
        """Legacy buyers: the dispatcher downgrades via our resolver even after a restart."""
        wire = {"products": [{"product_id": "p1", "format_ids": [_ref("audio_vast")]}]}
        canonical = bridge.canonicalize_wire_response("get_products", wire)
        bridge.reset_for_tests()  # simulate a fresh process: only the catalog remains
        legacy = project_canonical_response_to_legacy(canonical, resolver=bridge.canonical_format_legacy_resolver)
        assert legacy["products"][0]["format_ids"] == [_ref("audio_vast")]
        assert "format_options" not in legacy["products"][0]


class TestHandlerDispatch:
    """A canonical sync_creatives request reaches the impl as a legacy creative."""

    def test_sync_creatives_canonical_request_reaches_impl_with_format_id(self):
        from adcp.decisioning.serve import create_adcp_server_from_platform
        from adcp.server.base import ToolContext

        from core.platforms.mock import MockSellerPlatform
        from src.core.schemas import SyncCreativesResponse
        from tests.helpers.core_platform import make_active_tenant_session

        option_id = bridge.declaration_for_legacy_ref(_ref("display_300x250")).format_option_id
        impl_response = SyncCreativesResponse(creatives=[], failed_creatives=[])
        impl_mock = MagicMock(return_value=impl_response)
        with (
            patch("core.stores.accounts.get_db_session", return_value=make_active_tenant_session()),
            patch("core.platforms._delegate._sync_creatives_impl", new=impl_mock),
            patch(
                "core.platforms._delegate.get_tenant_by_id", return_value={"tenant_id": "demo-tenant", "name": "Demo"}
            ),
            # Account enrichment and webhook emission hit the DB; out of scope here.
            patch(
                "core.platforms._delegate.enrich_identity_with_account", side_effect=lambda identity, _account: identity
            ),
            patch("core.platforms._delegate._emit_creative_created_for_new_creatives"),
        ):
            platform = MockSellerPlatform()
            handler, _executor, _registry = create_adcp_server_from_platform(
                platform, auto_emit_completion_webhooks=False
            )
            request = {
                "account": {"account_id": "demo-tenant:demo"},
                "idempotency_key": "11111111-1111-4111-8111-111111111111",
                "creatives": [
                    {
                        "creative_id": "c1",
                        "name": "Banner",
                        "format_kind": "image",
                        "format_option_ref": {"scope": "product", "format_option_id": option_id},
                        "assets": {
                            "image": {
                                "asset_type": "image",
                                "url": "https://cdn.example/b.png",
                                "width": 300,
                                "height": 250,
                            }
                        },
                    }
                ],
            }
            # The dispatcher validates canonical wire params into the SDK's model
            # before invoking the handler; do the same here.
            from adcp.types import SyncCreativesRequest as CanonicalSyncCreativesRequest

            params = CanonicalSyncCreativesRequest.model_validate(request)
            result = asyncio.run(handler.sync_creatives(params, ToolContext()))
        assert isinstance(result, dict)
        forwarded = impl_mock.call_args.kwargs["creatives"][0]
        assert forwarded["format_id"] == _ref("display_300x250")
        assert "format_kind" not in forwarded and "format_option_ref" not in forwarded


class TestDeprecatedShapesUpgradedBeforeSdk:
    """Pre-validation compat: deprecated format shapes become structured tuples
    so the adcp 7 dispatcher can project them (it rejects bare strings)."""

    def test_bare_string_creative_format_id(self):
        from src.core.request_compat import normalize_request_params

        out = normalize_request_params(
            "sync_creatives", {"creatives": [{"creative_id": "c1", "format_id": "display_300x250_image"}]}
        )
        assert out.params["creatives"][0]["format_id"] == {
            "agent_url": AAO,
            "id": "display_image",
            "width": 300,
            "height": 250,
        }
        assert "format_id string → structured format_id" in out.translations_applied

    def test_legacy_key_creative_format_id(self):
        from src.core.request_compat import normalize_request_params

        out = normalize_request_params(
            "sync_creatives",
            {"creatives": [{"creative_id": "c1", "format_id": {"agent_url": AAO, "format_id": "display_image"}}]},
        )
        assert out.params["creatives"][0]["format_id"] == {"agent_url": AAO, "id": "display_image"}

    def test_get_products_filter_strings(self):
        from src.core.request_compat import normalize_request_params

        out = normalize_request_params("get_products", {"filters": {"format_ids": ["display_300x250_image"]}})
        assert out.params["filters"]["format_ids"][0]["id"] == "display_image"

    def test_structured_tuple_untouched(self):
        from src.core.request_compat import normalize_request_params

        ref = {"agent_url": AAO, "id": "display_image", "width": 300, "height": 250}
        out = normalize_request_params("sync_creatives", {"creatives": [{"creative_id": "c1", "format_id": ref}]})
        assert out.params["creatives"][0]["format_id"] == ref
        assert out.translations_applied == []


class TestA2AFloatDimensions:
    def test_request_compat_coerces_integral_floats(self):
        from src.core.request_compat import normalize_request_params

        ref = {"agent_url": AAO, "id": "display_image", "width": 300.0, "height": 250.0}
        out = normalize_request_params("sync_creatives", {"creatives": [{"creative_id": "c1", "format_id": ref}]})
        assert out.params["creatives"][0]["format_id"] == {
            "agent_url": AAO,
            "id": "display_image",
            "width": 300,
            "height": 250,
        }

    def test_format_id_schema_coerces_integral_floats(self):
        from src.core.schemas import FormatId

        assert FormatId(agent_url=AAO, id="display_image", width=300.0, height=250.0).width == 300


class TestPreValidationPreparation:
    """``prepare_legacy_request`` runs before the SDK negotiates a creative dialect."""

    def test_versionless_legacy_request_gets_3_0_hint_and_indexes_tuples(self):
        custom = {"agent_url": "https://example.com/agent", "id": "display_300x250"}
        out = bridge.prepare_legacy_request(
            "sync_creatives", {"creatives": [{"creative_id": "c1", "format_id": custom}]}
        )
        assert out["adcp_version"] == "3.0"
        assert bridge.resolve_option_id(migrated_format_option_id(LegacyFormatId(**custom))).id == "display_300x250"

    def test_explicit_version_is_kept(self):
        out = bridge.prepare_legacy_request(
            "sync_creatives",
            {"adcp_version": "3.1", "creatives": [{"creative_id": "c1", "format_id": _ref("display_300x250")}]},
        )
        assert out["adcp_version"] == "3.1"

    def test_versionless_canonical_request_defaults_to_3_1(self):
        params = {"creatives": [{"creative_id": "c1", "format_kind": "image"}]}
        assert bridge.prepare_legacy_request("sync_creatives", params) == {**params, "adcp_version": "3.1"}

    def test_versionless_request_without_creative_identity_defaults_to_3_1(self):
        out = bridge.prepare_legacy_request("get_products", {"buying_mode": "brief", "brief": "x"})
        assert out["adcp_version"] == "3.1"

    def test_non_creative_tools_untouched(self):
        params = {"media_buy_ids": ["mb_1"]}
        assert bridge.prepare_legacy_request("get_media_buy_delivery", params) == params


class TestConverterHeuristics:
    def test_plain_dict_catalog_entry_uses_id_heuristics(self):
        body = bridge.canonical_body_for_format(
            {"id": "display_300x250", "name": "Display 300x250"},
            LegacyFormatId(agent_url="https://example.com/agent", id="display_300x250"),
        )
        assert body == {"format_kind": "image", "params": {"width": 300, "height": 250}}

    def test_unknown_id_without_assets_is_none(self):
        body = bridge.canonical_body_for_format(
            {"id": "mystery"}, LegacyFormatId(agent_url="https://example.com/agent", id="mystery")
        )
        assert body is None


class TestCapabilityDeclaration:
    def test_features_declare_canonical_creatives_for_dialect_resolution(self):
        from adcp.canonical_formats.dialect import canonical_creatives_capability, resolve_creative_dialect
        from adcp.decisioning.capabilities import MediaBuy

        media_buy = MediaBuy(
            supported_pricing_models=["cpm"],
            features=bridge.CanonicalCreativeFeatures(inline_creative_management=True),
        )
        assert canonical_creatives_capability({"media_buy": media_buy}) is True
        dialect = resolve_creative_dialect("3.1", capabilities={"media_buy": media_buy}, request={"brief": "x"})
        assert dialect.value == "canonical"


class TestAdapterSchemeFormats:
    """Ad-server-owned formats use a non-HTTP ``agent_url`` (creatives/_validation.py).

    adcp 7 only projects legacy tuples whose owner is a public HTTPS host, so
    the bridge spells such owners synthetically for the SDK and maps back.
    """

    ADAPTER_REF = {"agent_url": "adapter-test://default", "id": "legacy_adapter_format"}

    def test_owner_encoding_round_trips(self):
        encoded = bridge._encode_adapter_owner(self.ADAPTER_REF["agent_url"])
        assert encoded.startswith("https://")
        assert bridge._decode_adapter_owner(encoded) == "adapter-test://default"
        assert bridge._decode_adapter_owner("https://creative.adcontextprotocol.org/") is None

    def test_prepare_legacy_request_respells_adapter_owner_and_hints_legacy(self):
        params = {"creatives": [{"creative_id": "c1", "format_id": dict(self.ADAPTER_REF), "assets": {}}]}
        out = bridge.prepare_legacy_request("sync_creatives", params)
        assert out["adcp_version"] == "3.0"
        agent_url = out["creatives"][0]["format_id"]["agent_url"]
        assert agent_url.startswith("https://") and bridge._decode_adapter_owner(agent_url) == "adapter-test://default"
        # the buyer's dict is not mutated
        assert params["creatives"][0]["format_id"] == self.ADAPTER_REF

    def test_converter_maps_synthetic_owner_back_to_original_tuple(self):
        synthetic = bridge.legacy_ref(
            {**self.ADAPTER_REF, "agent_url": bridge._encode_adapter_owner("adapter-test://default")}
        )
        body = bridge.legacy_format_converter(LegacyFormatConversionContext(synthetic, "", "creatives[0].format_id"))
        assert body is not None
        assert body["format_kind"] == "custom" and body["format_shape"] == "legacy_adapter_format"
        resolved = bridge.resolve_option_id(body["format_option_id"])
        assert resolved is not None
        assert (str(resolved.agent_url), resolved.id) == ("adapter-test://default", "legacy_adapter_format")

    def test_declaration_for_adapter_ref_is_custom_and_resolves_back(self):
        declaration = bridge.declaration_for_legacy_ref(self.ADAPTER_REF)
        assert declaration is not None
        assert declaration.format_kind.value == "custom" and declaration.format_shape == "legacy_adapter_format"
        resolved = bridge.resolve_option_id(declaration.format_option_id)
        assert resolved is not None and str(resolved.agent_url) == "adapter-test://default"

    def test_sync_creatives_round_trip_restores_adapter_format_id(self):
        option_id = bridge.declaration_for_legacy_ref(self.ADAPTER_REF).format_option_id
        creative = {
            "creative_id": "c1",
            "format_kind": "custom",
            "format_option_ref": {
                "scope": "publisher",
                "publisher_domain": "seller.example",
                "format_option_id": option_id,
            },
        }
        out = bridge.legacy_request_payload("sync_creatives", {"creatives": [creative]})["creatives"][0]
        assert out["format_id"]["agent_url"] == "adapter-test://default"
        assert out["format_id"]["id"] == "legacy_adapter_format"
