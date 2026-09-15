"""Slim MCP input schemas for tools whose auto-generated schemas are too large.

The adcp library generates input schemas from Pydantic models via
``_generate_pydantic_schemas()`` and then inlines all ``$ref`` nodes via
``_inline_refs()``.  Because every asset-bearing model inlines the full
creative asset union, the result is enormous.  Approximate served cost of
one booking flow (tokens, at ~4 chars/token) before and after slimming:

===================== ============ ===========
tool                          full        slim
===================== ============ ===========
sync_creatives             480 036       ~1 000
create_media_buy           546 896       ~1 300
update_media_buy         1 045 087       ~1 300
get_products                45 972       ~1 000
===================== ============ ===========

Either volume alone fills an LLM context window on ``tools/list`` before any
useful work can happen.

This module provides hand-crafted replacements that:
* Cover every **required** field (enforced by ``test_slim_schema_guard.py``).
* Include the most commonly used optional fields (ranked by test-corpus usage).
* Stay under ~100 lines so the schema is a negligible context cost.
* Advertise **only** fields the request model actually accepts — a property
  with no matching model field is not merely dead weight, it makes agents
  send it and get rejected by ``mcp_compat_middleware`` outside production.

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
        # NOTE: no `name` field. CreateMediaBuyRequest has no `name`, so
        # advertising one makes agents send it and get hard-rejected by
        # mcp_compat_middleware ("Unknown field(s) for create_media_buy: name")
        # outside production. Campaign naming is derived server-side.
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
                            "Inline creatives (one-shot upload + assign). Alternatively call "
                            "sync_creatives first and pass only creative_id + name here.\n"
                            "Supply exactly one of format_id or format_kind. Reuse the "
                            "format_id verbatim from the chosen product's format_ids "
                            "(get_products) — no list_creative_formats call needed.\n"
                            "assets keys are the format's slot names, e.g. "
                            "banner_image: {asset_type:'image', url, width, height}; "
                            "click_url: {asset_type:'url', url, url_type:'clickthrough'}."
                        ),
                        "items": {
                            "type": "object",
                            "required": ["creative_id", "name", "assets"],
                            "properties": {
                                "creative_id": {"type": "string"},
                                "name": {"type": "string"},
                                # format_id: legacy named-format path. Copy the object
                                # straight out of product.format_ids[] — it already
                                # carries agent_url, id and any width/height parameters.
                                "format_id": {
                                    "type": "object",
                                    "description": (
                                        "Copy an entry from the product's format_ids[] as-is, "
                                        "including width/height when present."
                                    ),
                                    "properties": {
                                        "agent_url": {"type": "string"},
                                        "id": {"type": "string"},
                                        "width": {"type": "integer"},
                                        "height": {"type": "integer"},
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
# get_products
# ---------------------------------------------------------------------------
# Required fields (1):  buying_mode
# `fields` is deliberately typed as a plain string array with `examples`
# rather than a hard enum: the enum has grown across adcp releases (30 -> 39
# values), so inlining it risks advertising values the running server rejects
# with VALIDATION_ERROR. Unknown values are rejected, so prefer omitting
# `fields` entirely over guessing.
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
                "'brief': publisher curates from a natural-language brief (requires brief). "
                "'wholesale': raw product feed, no brief, returns no proposals. "
                "'refine': iterate on a previous response (requires refine)."
            ),
        },
        # ── mode-specific ──────────────────────────────────────────────────
        "brief": {
            "type": "string",
            "description": (
                "Natural-language campaign requirements. Required when "
                "buying_mode='brief'; must be omitted for 'wholesale' and 'refine'."
            ),
        },
        "refine": {
            "type": "array",
            "description": (
                "Change requests against a previous response. Only valid when "
                "buying_mode='refine'. Also the way to fetch a known product_id."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["request", "product", "proposal"],
                    },
                    "product_id": {"type": "string"},
                    "proposal_id": {"type": "string"},
                    "action": {
                        "type": "string",
                        "description": (
                            "product scope: include|omit|more_like_this. "
                            "proposal scope: include|omit|finalize. "
                            "'finalize' must be the only action in the array."
                        ),
                    },
                    "ask": {"type": "string", "description": "What to change."},
                },
                "required": ["scope"],
            },
        },
        # ── common optional ────────────────────────────────────────────────
        "account": {
            "type": "object",
            "description": (
                "Account for account-specific rate-card pricing. "
                "Either {account_id: str} or {brand: {domain: str}, operator: str}."
            ),
        },
        "brand": {
            "type": "object",
            "description": "Brand reference for discovery context: {domain: 'example.com'}.",
        },
        "fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Response projection — omit to get all fields. Unknown values are "
                "rejected with VALIDATION_ERROR, so omit rather than guess. "
                "product_id and name are always returned."
            ),
            "examples": [
                [
                    "product_id",
                    "name",
                    "channels",
                    "format_ids",
                    "pricing_options",
                    "delivery_type",
                ]
            ],
        },
        "filters": {
            "type": "object",
            "description": "Narrow the catalog. Non-matching products are silently excluded.",
            "properties": {
                "channels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "e.g. ['display', 'ctv', 'olv', 'streaming_audio'].",
                },
                "delivery_type": {
                    "type": "string",
                    "enum": ["guaranteed", "non_guaranteed"],
                },
                "is_fixed_price": {"type": "boolean"},
                "countries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ISO 3166-1 alpha-2, e.g. ['NL', 'DE'].",
                },
                "pricing_currencies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ISO 4217, e.g. ['EUR'].",
                },
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "budget_range": {
                    "type": "object",
                    "properties": {
                        "currency": {"type": "string"},
                        "min": {"type": "number"},
                        "max": {"type": "number"},
                    },
                    "required": ["currency"],
                },
            },
        },
        "preferred_delivery_types": {
            "type": "array",
            "items": {"type": "string", "enum": ["guaranteed", "non_guaranteed"]},
            "description": "Preference hint (unlike filters.delivery_type, does not exclude).",
        },
        "pagination": {
            "type": "object",
            "description": "Cursor pagination: {cursor?: str, max_results?: int (1-100, default 50)}.",
            "properties": {
                "cursor": {"type": "string"},
                "max_results": {"type": "integer"},
            },
        },
    },
}


# ---------------------------------------------------------------------------
# sync_creatives
# ---------------------------------------------------------------------------
# Required fields (3):  account, creatives, idempotency_key
# Only needed for the two-step creative path (upload to library, then pass
# creative_id to create_media_buy). The one-shot path — inline creatives in
# create_media_buy's packages[].creatives — skips this tool entirely.
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
                "Re-send the same key to safely retry without syncing twice."
            ),
        },
        "creatives": {
            "type": "array",
            "description": (
                "Creatives to create or update (max 100). Idempotent per creative_id: "
                "re-sending an existing creative_id updates it."
            ),
            "items": {
                "type": "object",
                "required": ["creative_id", "name", "assets"],
                "properties": {
                    "creative_id": {
                        "type": "string",
                        "description": "Your stable identifier; the update key on re-sync.",
                    },
                    "name": {"type": "string"},
                    "format_id": {
                        "type": "object",
                        "description": (
                            "Copy an entry from the target product's format_ids[] as-is, "
                            "including width/height when present. "
                            "Mutually exclusive with format_kind."
                        ),
                        "properties": {
                            "agent_url": {"type": "string"},
                            "id": {"type": "string"},
                            "width": {"type": "integer"},
                            "height": {"type": "integer"},
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
                            "Slot values keyed by the format's slot names, e.g. "
                            "banner_image: {asset_type:'image', url, width, height}; "
                            "click_url: {asset_type:'url', url, url_type:'clickthrough'}."
                        ),
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Free-form tags for organisation and search.",
                    },
                },
            },
        },
        # ── common optional ────────────────────────────────────────────────
        "assignments": {
            "type": "array",
            "description": "Bulk-assign creatives to packages of an existing media buy.",
            "items": {
                "type": "object",
                "required": ["creative_id", "package_id"],
                "properties": {
                    "creative_id": {"type": "string"},
                    "package_id": {"type": "string"},
                    "weight": {
                        "type": "number",
                        "description": "Relative rotation weight 0-100. 0 = assigned but paused.",
                    },
                },
            },
        },
        "creative_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": ("Limit the sync to these creative_ids. Cannot be combined with delete_missing."),
        },
        "validation_mode": {
            "type": "string",
            "enum": ["strict", "lenient"],
            "description": (
                "'strict' (default) fails the whole sync on any error. "
                "'lenient' processes valid creatives and reports the rest."
            ),
        },
        "dry_run": {
            "type": "boolean",
            "description": "Preview what would change without applying it.",
        },
        "delete_missing": {
            "type": "boolean",
            "description": (
                "Archive every library creative absent from this call. Full-library replacement — use with care."
            ),
        },
        "push_notification_config": {
            "type": "object",
            "description": (
                "Webhook for async sync completion (large batches / manual review). "
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
