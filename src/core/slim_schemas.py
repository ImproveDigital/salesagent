"""Slim MCP input schemas for tools whose auto-generated schemas are too large.

The adcp library generates input schemas from Pydantic models via
``_generate_pydantic_schemas()`` and then inlines all ``$ref`` nodes via
``_inline_refs()``.  For ``create_media_buy`` this produces ~93 000 lines of
JSON that fills an LLM context window before any useful work can happen.
``update_media_buy`` is even larger (~4.2 MB inlined vs ~2.2 MB).

This module provides hand-crafted replacements that:
* Cover every **required** field (enforced by ``test_slim_schema_guard.py``).
* Include the most commonly used optional fields (ranked by test-corpus usage).
* Stay under ~100 lines so the schema is a negligible context cost.

Runtime behaviour is unchanged — the slim schema only affects what MCP
clients see during tool discovery; the underlying function still validates
against the full Pydantic request model.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# create_media_buy
# ---------------------------------------------------------------------------
# Required fields (5):  idempotency_key, account, brand, start_time, end_time
# Top optional fields by test-corpus frequency:
#   packages[].creatives (77 files), packages[].targeting_overlay (22 files),
#   po_number (19 files), push_notification_config (12 files),
#   packages[].pacing (8 files), reporting_webhook (7 files)
# ---------------------------------------------------------------------------

CREATE_MEDIA_BUY_SLIM_SCHEMA: dict = {
    "type": "object",
    "required": ["idempotency_key", "account", "brand", "start_time", "end_time"],
    "properties": {
        # ── required ──────────────────────────────────────────────────────
        "idempotency_key": {
            "type": "string",
            "description": (
                "Client-generated unique key (16-255 chars, alphanumeric + _.:-). "
                "Re-send the same key to safely retry without creating a duplicate."
            ),
        },
        "account": {
            "type": "object",
            "description": (
                "Account to bill. Either {account_id: str} or {brand: {domain: str}, operator: str, sandbox?: bool}."
            ),
        },
        "brand": {
            "type": "object",
            "description": "Brand reference. Provide {domain: 'example.com'}.",
        },
        "start_time": {
            "type": "string",
            "description": "Campaign start: ISO 8601 datetime or the literal string 'asap'.",
        },
        "end_time": {
            "type": "string",
            "format": "date-time",
            "description": "Campaign end: ISO 8601 datetime.",
        },
        # ── common optional (top-level) ────────────────────────────────────
        "name": {
            "type": "string",
            "description": "Human-readable campaign name.",
        },
        "po_number": {
            "type": "string",
            "description": "Purchase order number for tracking.",
        },
        "push_notification_config": {
            "type": "object",
            "description": (
                "Webhook for async task-completion notifications. "
                "Provide {url, authentication: {schemes, credentials}}."
            ),
            "properties": {
                "url": {"type": "string", "format": "uri"},
                "authentication": {"type": "object"},
            },
            "required": ["url"],
        },
        "reporting_webhook": {
            "type": "object",
            "description": (
                "Webhook for periodic delivery-metrics reports. Provide {url, authentication, reporting_frequency}."
            ),
            "properties": {
                "url": {"type": "string", "format": "uri"},
                "authentication": {"type": "object"},
                "reporting_frequency": {
                    "type": "string",
                    "enum": ["hourly", "daily", "monthly"],
                },
            },
            "required": ["url", "authentication", "reporting_frequency"],
        },
        # ── packages ───────────────────────────────────────────────────────
        "packages": {
            "type": "array",
            "description": "One entry per product/placement combination.",
            "items": {
                "type": "object",
                "required": ["product_id", "budget", "pricing_option_id"],
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "product_id from get_products.",
                    },
                    "budget": {
                        "type": "number",
                        "description": "Package budget in the account currency.",
                    },
                    "pricing_option_id": {
                        "type": "string",
                        "description": "pricing_option_id from the product's pricing_options.",
                    },
                    "start_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": "Package flight start (inherits media buy start if omitted).",
                    },
                    "end_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": "Package flight end (inherits media buy end if omitted).",
                    },
                    "pacing": {
                        "type": "string",
                        "enum": ["even", "asap", "front_loaded"],
                        "description": "Delivery pacing strategy.",
                    },
                    "targeting_overlay": {
                        "type": "object",
                        "description": "Targeting constraints for this package.",
                        "properties": {
                            "geo_countries": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "ISO 3166-1 alpha-2 country codes, e.g. ['NL', 'DE'].",
                            },
                            "geo_regions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "ISO 3166-2 codes, e.g. ['US-CA', 'GB-SCT'].",
                            },
                            "device_type": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": ["desktop", "mobile", "tablet", "ctv", "dooh"],
                                },
                            },
                            "frequency_cap": {
                                "type": "object",
                                "description": "Max impressions per entity per time window.",
                                "properties": {
                                    "max_impressions": {"type": "integer"},
                                    "window": {
                                        "type": "object",
                                        "properties": {
                                            "interval": {"type": "integer"},
                                            "unit": {
                                                "type": "string",
                                                "enum": ["hours", "days", "campaign"],
                                            },
                                        },
                                        "required": ["interval", "unit"],
                                    },
                                },
                            },
                        },
                    },
                    "creatives": {
                        "type": "array",
                        "description": (
                            "Inline creative assets to upload and assign to this package (one-shot path). "
                            "Two approaches:\n"
                            "  1. One-shot: include creatives here with format_id or format_kind + assets.\n"
                            "  2. Two-step: call sync_creatives first, then reference creative_id here "
                            "     (omit format_id/format_kind/assets).\n"
                            "format_id vs format_kind: use format_id {agent_url, id} when you need to "
                            "reference a named format from a specific creative agent. "
                            "Always call list_creative_formats first to discover the correct agent_url "
                            "(returned in creative_agents[].agent_url — typically "
                            "'https://creative.adcontextprotocol.org/'). "
                            "Use format_kind (enum string) for the simpler canonical-format path. "
                            "format_id and format_kind are mutually exclusive.\n"
                            "assets keys are slot names from the format (e.g. banner_image, click_url). "
                            "banner_image: {asset_type:'image', url, width, height}. "
                            "click_url: {asset_type:'url', url, url_type:'clickthrough'}."
                        ),
                        "items": {
                            "type": "object",
                            "required": ["creative_id", "name", "assets"],
                            "properties": {
                                "creative_id": {"type": "string"},
                                "name": {"type": "string"},
                                # format_id: legacy named-format path — agent_url discovered via
                                # list_creative_formats creative_agents[].agent_url
                                "format_id": {
                                    "type": "object",
                                    "description": (
                                        "Named-format path. Always {agent_url, id}. "
                                        "agent_url MUST be discovered from list_creative_formats "
                                        "response's creative_agents[].agent_url "
                                        "(e.g. 'https://creative.adcontextprotocol.org/'). "
                                        "Mutually exclusive with format_kind."
                                    ),
                                    "properties": {
                                        "agent_url": {
                                            "type": "string",
                                            "description": (
                                                "URL of the agent that owns this format. "
                                                "Discover via list_creative_formats creative_agents[].agent_url. "
                                                "Example: 'https://creative.adcontextprotocol.org/'"
                                            ),
                                        },
                                        "id": {
                                            "type": "string",
                                            "description": "Format ID, e.g. 'display_300x250'.",
                                        },
                                    },
                                    "required": ["agent_url", "id"],
                                },
                                # format_kind: 3.1+ canonical-format path — simpler alternative to format_id
                                "format_kind": {
                                    "type": "string",
                                    "description": "Canonical format name. Mutually exclusive with format_id.",
                                    "enum": [
                                        "image",
                                        "html5",
                                        "display_tag",
                                        "video_hosted",
                                        "video_vast",
                                        "audio_hosted",
                                        "native_in_feed",
                                    ],
                                },
                                "assets": {
                                    "type": "object",
                                    "description": (
                                        "Slot values keyed by slot name from the format. "
                                        "banner_image: {asset_type:'image', url, width, height}. "
                                        "click_url: {asset_type:'url', url, url_type:'clickthrough'}."
                                    ),
                                },
                            },
                        },
                    },
                },
            },
        },
        # ── proposal shortcut ──────────────────────────────────────────────
        "proposal_id": {
            "type": "string",
            "description": (
                "Execute a committed proposal instead of specifying packages manually. "
                "Pair with total_budget to derive package budgets from allocation percentages."
            ),
        },
        "total_budget": {
            "type": "object",
            "description": "Total budget when executing a proposal: {amount: number, currency: str}.",
            "properties": {
                "amount": {"type": "number"},
                "currency": {"type": "string"},
            },
            "required": ["amount", "currency"],
        },
    },
}


# ---------------------------------------------------------------------------
# update_media_buy
# ---------------------------------------------------------------------------
# Required fields (3):  account, media_buy_id, idempotency_key
# All other fields are optional — update is a partial patch (adcp 3.9+).
# Top optional fields by test-corpus frequency:
#   packages (53 files), start_time/end_time (34), paused (27),
#   packages[].creative_ids (15), canceled (8),
#   push_notification_config (6), reporting_webhook (3)
# NOTE: top-level `budget` is intentionally absent — the request model
# rejects it (AdCP has no media-buy-level budget update). Use per-package
# `budget` or `ext.salesagent.budget` instead. See UpdateMediaBuyRequest.
# ---------------------------------------------------------------------------

UPDATE_MEDIA_BUY_SLIM_SCHEMA: dict = {
    "type": "object",
    "required": ["account", "media_buy_id", "idempotency_key"],
    "properties": {
        # ── required ──────────────────────────────────────────────────────
        "account": {
            "type": "object",
            "description": (
                "Account that owns the media buy. Either {account_id: str} or "
                "{brand: {domain: str}, operator: str, sandbox?: bool}."
            ),
        },
        "media_buy_id": {
            "type": "string",
            "description": "ID of the media buy to update (from create_media_buy).",
        },
        "idempotency_key": {
            "type": "string",
            "description": (
                "Client-generated unique key (16-255 chars, alphanumeric + _.:-). "
                "Re-send the same key to safely retry without applying the update twice."
            ),
        },
        # ── media-buy-level updates (all optional) ─────────────────────────
        "paused": {
            "type": "boolean",
            "description": "Pause (true) or resume (false) the entire media buy.",
        },
        "canceled": {
            "type": "boolean",
            "enum": [True],
            "description": "Set to true to request cancellation of the media buy. Omit otherwise.",
        },
        "cancellation_reason": {
            "type": "string",
            "description": "Optional free-text reason when canceled=true.",
        },
        "start_time": {
            "type": "string",
            "description": "New campaign start: ISO 8601 datetime or the literal string 'asap'.",
        },
        "end_time": {
            "type": "string",
            "format": "date-time",
            "description": "New campaign end: ISO 8601 datetime.",
        },
        "push_notification_config": {
            "type": "object",
            "description": (
                "Webhook for async task-completion notifications. "
                "Provide {url, authentication: {schemes, credentials}}."
            ),
            "properties": {
                "url": {"type": "string", "format": "uri"},
                "authentication": {"type": "object"},
            },
            "required": ["url"],
        },
        "reporting_webhook": {
            "type": "object",
            "description": (
                "Webhook for periodic delivery-metrics reports. Provide {url, authentication, reporting_frequency}."
            ),
            "properties": {
                "url": {"type": "string", "format": "uri"},
                "authentication": {"type": "object"},
                "reporting_frequency": {
                    "type": "string",
                    "enum": ["hourly", "daily", "monthly"],
                },
            },
            "required": ["url", "authentication", "reporting_frequency"],
        },
        "ext": {
            "type": "object",
            "description": (
                "Extension object. To change total budget (no media-buy-level budget exists in AdCP), "
                "set {salesagent: {budget: number}} while adcontextprotocol/adcp#4241 is open."
            ),
        },
        # ── per-package updates ────────────────────────────────────────────
        "packages": {
            "type": "array",
            "description": (
                "Partial updates to existing packages. One entry per package; only include "
                "the fields you want to change. package_id is required to identify the package."
            ),
            "items": {
                "type": "object",
                "required": ["package_id"],
                "properties": {
                    "package_id": {
                        "type": "string",
                        "description": "ID of the package to update.",
                    },
                    "budget": {
                        "type": "number",
                        "description": "New package budget in the account currency.",
                    },
                    "impressions": {
                        "type": "number",
                        "description": "New impression goal for the package.",
                    },
                    "bid_price": {
                        "type": "number",
                        "description": "New bid price for the package.",
                    },
                    "pacing": {
                        "type": "string",
                        "enum": ["even", "asap", "front_loaded"],
                        "description": "Delivery pacing strategy.",
                    },
                    "paused": {
                        "type": "boolean",
                        "description": "Pause (true) or resume (false) this package.",
                    },
                    "start_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": "New package flight start.",
                    },
                    "end_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": "New package flight end.",
                    },
                    "creative_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Replace the package's assigned creatives with these creative_ids "
                            "(from sync_creatives / list_creatives)."
                        ),
                    },
                    "targeting_overlay": {
                        "type": "object",
                        "description": "Replacement targeting constraints for this package.",
                    },
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# sync_creatives
# ---------------------------------------------------------------------------
# Required fields (3):  account, creatives, idempotency_key
# (see src.core.schemas.creative.SyncCreativesRequest — creatives is re-typed
# against the local CreativeAsset but the required set matches the library)
# ---------------------------------------------------------------------------

SYNC_CREATIVES_SLIM_SCHEMA: dict = {
    "type": "object",
    "required": ["account", "creatives", "idempotency_key"],
    "properties": {
        # ── required ──────────────────────────────────────────────────────
        "account": {
            "type": "object",
            "description": (
                "Account that owns these creatives. Either {account_id: str} or "
                "{brand: {domain: str}, operator: str, sandbox?: bool}."
            ),
        },
        "idempotency_key": {
            "type": "string",
            "description": (
                "Client-generated unique key (16-255 chars, alphanumeric + _.:-). "
                "Re-send the same key to safely retry without applying the sync twice."
            ),
        },
        "creatives": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "description": (
                "Creative assets to create or update.\n"
                "format_id vs format_kind: use format_id {agent_url, id} to reference "
                "a named format from a specific creative agent. Always call "
                "list_creative_formats first to discover the correct agent_url "
                "(returned in creative_agents[].agent_url — typically "
                "'https://creative.adcontextprotocol.org/'). "
                "Use format_kind (enum string) for the simpler canonical-format path. "
                "format_id and format_kind are mutually exclusive.\n"
                "assets keys are slot names from the format (e.g. banner_image, click_url). "
                "banner_image: {asset_type:'image', url, width, height}. "
                "click_url: {asset_type:'url', url, url_type:'clickthrough'}."
            ),
            "items": {
                "type": "object",
                "required": ["creative_id", "name", "assets"],
                "properties": {
                    "creative_id": {
                        "type": "string",
                        "description": "Buyer-chosen stable ID for this creative.",
                    },
                    "name": {"type": "string"},
                    "format_id": {
                        "type": "object",
                        "description": (
                            "Named-format path. Always {agent_url, id}. "
                            "agent_url MUST be discovered from list_creative_formats "
                            "response's creative_agents[].agent_url. "
                            "Mutually exclusive with format_kind."
                        ),
                        "properties": {
                            "agent_url": {
                                "type": "string",
                                "description": (
                                    "URL of the agent that owns this format. "
                                    "Discover via list_creative_formats creative_agents[].agent_url."
                                ),
                            },
                            "id": {
                                "type": "string",
                                "description": "Format ID, e.g. 'display_300x250'.",
                            },
                        },
                        "required": ["agent_url", "id"],
                    },
                    "format_kind": {
                        "type": "string",
                        "description": "Canonical format name. Mutually exclusive with format_id.",
                        "enum": [
                            "image",
                            "html5",
                            "display_tag",
                            "video_hosted",
                            "video_vast",
                            "audio_hosted",
                            "native_in_feed",
                        ],
                    },
                    "assets": {
                        "type": "object",
                        "description": (
                            "Slot values keyed by slot name from the format. "
                            "banner_image: {asset_type:'image', url, width, height}. "
                            "click_url: {asset_type:'url', url, url_type:'clickthrough'}."
                        ),
                    },
                },
            },
        },
        # ── common optional ─────────────────────────────────────────────────
        "assignments": {
            "type": "array",
            "description": ("Bulk assignment of creatives to packages. Each entry maps one creative to one package."),
            "items": {
                "type": "object",
                "required": ["creative_id", "package_id"],
                "properties": {
                    "creative_id": {"type": "string"},
                    "package_id": {"type": "string"},
                    "weight": {
                        "type": "number",
                        "description": "Relative rotation weight within the package.",
                    },
                },
            },
        },
        "creative_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional filter to limit sync scope to these creative IDs. Invalid together with delete_missing."
            ),
        },
        "delete_missing": {
            "type": "boolean",
            "description": (
                "When true, creatives not included in this sync are archived "
                "(full library replacement — use with caution)."
            ),
        },
        "dry_run": {
            "type": "boolean",
            "description": "Preview changes without applying them.",
        },
        "validation_mode": {
            "type": "string",
            "enum": ["strict", "lenient"],
            "description": (
                "'strict' fails the entire sync on any validation error; "
                "'lenient' processes valid creatives and reports errors."
            ),
        },
        "push_notification_config": {
            "type": "object",
            "description": (
                "Webhook for async sync-completion notifications. "
                "Provide {url, authentication: {schemes, credentials}}."
            ),
            "properties": {
                "url": {"type": "string", "format": "uri"},
                "authentication": {"type": "object"},
            },
            "required": ["url"],
        },
    },
}


# ---------------------------------------------------------------------------
# get_products
# ---------------------------------------------------------------------------
# Required fields (1):  buying_mode
# brief is conditionally required (buying_mode='brief') and must be absent for
# 'wholesale', so it stays optional here — the Pydantic model enforces the
# cross-field rule at runtime.
# ---------------------------------------------------------------------------

GET_PRODUCTS_SLIM_SCHEMA: dict = {
    "type": "object",
    "required": ["buying_mode"],
    "properties": {
        # ── required ──────────────────────────────────────────────────────
        "buying_mode": {
            "type": "string",
            "enum": ["brief", "wholesale", "refine"],
            "description": (
                "Buyer intent. 'brief': publisher curates recommendations from the "
                "provided brief (brief is required). 'wholesale': full catalog with "
                "rate-card pricing (brief must be omitted). 'refine': iterate on a "
                "previous response via the refine array."
            ),
        },
        # ── common optional ─────────────────────────────────────────────────
        "brief": {
            "type": "string",
            "description": (
                "Natural-language campaign requirements. Required when "
                "buying_mode='brief'; must be omitted for 'wholesale'."
            ),
        },
        "brand": {
            "type": "object",
            "description": "Brand reference for discovery context. Provide {domain: 'example.com'}.",
        },
        "account": {
            "type": "object",
            "description": (
                "Account for product lookup — returns pricing from this account's "
                "rate card. Either {account_id: str} or "
                "{brand: {domain: str}, operator: str, sandbox?: bool}."
            ),
        },
        "filters": {
            "type": "object",
            "description": "Structured product filters — non-matching products are excluded.",
            "properties": {
                "delivery_type": {
                    "type": "string",
                    "enum": ["guaranteed", "non_guaranteed"],
                },
                "format_ids": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Restrict to products supporting these format IDs ({agent_url, id}).",
                },
                "is_fixed_price": {"type": "boolean"},
                "countries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ISO 3166-1 alpha-2 country codes, e.g. ['NL', 'DE'].",
                },
                "channels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Inventory channels, e.g. ['display', 'video', 'ctv'].",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
        "pagination": {
            "type": "object",
            "description": "Cursor pagination: {limit: int, offset: int}.",
            "properties": {
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Tool-definition compaction
# ---------------------------------------------------------------------------

# tool name → slim replacement for its adcp-generated inputSchema.
SLIM_INPUT_SCHEMAS: dict[str, dict] = {
    "create_media_buy": CREATE_MEDIA_BUY_SLIM_SCHEMA,
    "update_media_buy": UPDATE_MEDIA_BUY_SLIM_SCHEMA,
    "sync_creatives": SYNC_CREATIVES_SLIM_SCHEMA,
    "get_products": GET_PRODUCTS_SLIM_SCHEMA,
}


def compact_tool_schemas(tool_defs: list[dict]) -> None:
    """Compact adcp tool definitions in place for ``tools/list``.

    Two reductions, applied to the mutable ``ADCP_TOOL_DEFINITIONS`` list:

    * ``inputSchema`` — replaced with the hand-written slim schema for the
      tools in ``SLIM_INPUT_SCHEMAS`` (the asset-bearing booking-flow tools
      whose inlined schemas run to megabytes).
    * ``outputSchema`` — dropped from **every** tool.  The inlined output
      schemas total ~4.7 MB across the advertised tools — 92% of the
      ``tools/list`` payload — which pushed the response past buyer-agent
      size caps (Scope3 rejects responses over 5 MB, blocking catalog
      discovery entirely).  ``outputSchema`` is optional in MCP; without a
      spec schema, adcp's ``_register_tool`` falls back to the generic
      object schema derived from the wrapper's ``-> dict[str, Any]`` return
      annotation, so ``structuredContent`` is still populated on responses.

    Runtime request validation is unchanged — tools still validate against
    the full Pydantic request models.
    """
    for tool in tool_defs:
        slim = SLIM_INPUT_SCHEMAS.get(tool["name"])
        if slim is not None:
            tool["inputSchema"] = slim
        tool.pop("outputSchema", None)
