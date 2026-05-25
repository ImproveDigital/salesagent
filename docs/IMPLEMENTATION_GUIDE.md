# Prebid Sales Agent — Implementation Guide

A complete reference for developers working on this codebase. Covers architecture, every major subsystem, capabilities, and the things that do **not** exist (so you know what to build versus what to use).

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Startup & Entry Points](#2-startup--entry-points)
3. [Transport Layer — Three Protocols](#3-transport-layer--three-protocols)
4. [Identity Resolution & Authentication](#4-identity-resolution--authentication)
5. [MCP Tools — Complete Reference](#5-mcp-tools--complete-reference)
6. [Adapter Architecture](#6-adapter-architecture)
7. [Database Layer](#7-database-layer)
8. [Repository Pattern](#8-repository-pattern)
9. [Schema Layer (Pydantic)](#9-schema-layer-pydantic)
10. [Services Layer](#10-services-layer)
11. [Admin UI](#11-admin-ui)
12. [Multi-Tenancy](#12-multi-tenancy)
13. [Workflow / HITL System](#13-workflow--hitl-system)
14. [AI Agents](#14-ai-agents)
15. [Background Schedulers](#15-background-schedulers)
16. [Testing Infrastructure](#16-testing-infrastructure)
17. [Configuration & Environment](#17-configuration--environment)
18. [What Is and Is Not Possible](#18-what-is-and-is-not-possible)
19. [Adding New Features — Decision Trees](#19-adding-new-features--decision-trees)

---

## 1. System Overview

The Prebid Sales Agent is a **multi-tenant AI-powered campaign management platform**. It acts as an intermediary between AI agents (LLMs, autonomous buyers) and real ad servers. Its job is to:

- Accept natural-language or structured campaign briefs
- Translate them into ad-server-specific orders/line items
- Track delivery and status asynchronously
- Expose everything through standardised AdCP protocol

### High-level topology

```
                   ┌──────────────────────────────────────────────┐
AI Agent / LLM ───►│  nginx  (port 8000)                         │
                   │  ├── /mcp/     → FastMCP (MCP protocol)     │
                   │  ├── /a2a      → A2A server (agent-to-agent) │
                   │  ├── /admin/   → Admin UI (Flask)            │
                   │  └── /api/     → REST compat layer (FastAPI) │
                   └──────────────────────────────────────────────┘
                                   │
                       ┌───────────┼───────────┐
                       ▼           ▼           ▼
                   PostgreSQL   Ad Server   AI Services
                               Adapters    (Gemini/Claude)
                    (models)   GAM / Mock
                               Kevel / etc.
```

### Key design decisions

| Decision | Rationale |
|----------|-----------|
| AdCP protocol compliance | Standardised protocol so any conforming AI agent works |
| Transport-agnostic `_impl` functions | Business logic is not tied to MCP, A2A, or REST |
| Repository pattern | Database access is never inline in business logic |
| Pluggable adapters | Swap ad servers without changing business logic |
| Multi-tenant from day one | Each tenant gets isolated data, adapter config, and products |
| PostgreSQL-only | Production system; SQLite is never supported |

---

## 2. Startup & Entry Points

### Primary entry: `src/app.py`

The FastAPI application that mounts every sub-application:

```
src/app.py
  ├── mounts FastMCP app at /mcp/
  ├── mounts A2A server at /a2a
  ├── mounts Flask admin UI at /admin/
  ├── mounts landing page at /
  └── registers REST routes from src/routes/
```

### MCP server initialisation: `src/core/main.py`

1. Loads tenant config (falls back to minimal mock config if DB unavailable)
2. Creates `FastMCP("AdCPSalesAgent")` instance with `lifespan_context`
3. Adds middleware: `MCPAuthMiddleware`, `RequestCompatMiddleware`
4. Imports and registers 16 MCP tools with the `with_error_logging` wrapper
5. Defines `get_product_catalog()` and `get_strategy_manager()` helpers

### Lifespan (startup / shutdown)

On **startup**:
1. `start_delivery_webhook_scheduler()` — async webhook dispatch loop
2. `start_media_buy_status_scheduler()` — periodic ad-server polling

On **shutdown**:
1. Stop media buy status scheduler
2. Stop delivery webhook scheduler

### Database initialisation

`src/core/database/database.py → init_db()` creates all tables from SQLAlchemy metadata. Runs automatically on server start via `scripts/ops/migrate.py`.

---

## 3. Transport Layer — Three Protocols

Every feature is exposed through three parallel transports. Each transport resolves identity and calls the shared `_impl` function.

### 3.1 MCP (Model Context Protocol) — `/mcp/`

- Built on **FastMCP** (`fastmcp>=3.2.0`)
- AI agents call tools by name with JSON parameters
- Authentication via `x-adcp-auth` or `Authorization: Bearer` header
- All tools wrapped with `with_error_logging` for activity feed visibility
- Stateless HTTP mode (no persistent sessions required)

### 3.2 A2A (Agent-to-Agent) — `/a2a`

- Built on **python-a2a** (`a2a-sdk>=0.3.19`)
- `src/a2a_server/adcp_a2a_server.py` — main server
- `src/a2a_server/context_builder.py` — builds request context from A2A messages
- Same business logic as MCP; different serialisation

### 3.3 REST — `/api/v1/`

- Built on **FastAPI**
- `src/routes/api_v1.py` — REST endpoints
- `src/routes/rest_compat_middleware.py` — compatibility shim for old clients
- Health checks at `src/routes/health.py`

### Transport boundary rule

**Every transport** must:
1. Call `resolve_identity(headers, protocol="mcp"|"a2a"|"rest")` → `ResolvedIdentity`
2. Pass `identity` to the `_impl` function
3. Translate `AdCPError` subclasses into transport-appropriate error format

**`_impl` functions** must:
- Accept `ResolvedIdentity`, never `Context` / `ToolContext`
- Raise `AdCPError` subclasses, never `ToolError`
- Have zero imports from `fastmcp`, `a2a`, `starlette`, `fastapi`

---

## 4. Identity Resolution & Authentication

### `ResolvedIdentity` — `src/core/resolved_identity.py`

Immutable Pydantic model created at each transport boundary:

```python
class ResolvedIdentity(BaseModel, frozen=True):
    principal_id: str | None    # validated principal
    tenant_id:    str | None    # resolved tenant
    tenant:       Any           # TenantContext (or raw dict during transition)
    auth_token:   str | None    # raw token for downstream use
    protocol:     Literal["mcp", "a2a", "rest"]
    testing_context: AdCPTestContext | None
    account_id:   str | None    # pre-resolved AccountReference
```

### Tenant detection — four strategies (in priority order)

1. **Host header** → virtual host DB lookup, then subdomain extraction
2. **`x-adcp-tenant` header** → subdomain lookup, then direct tenant_id
3. **`Apx-Incoming-Host` header** → Approximated.app virtual host
4. **`localhost` fallback** → resolves to `"default"` tenant

### Token validation

- Header: `x-adcp-auth: <token>` or `Authorization: Bearer <token>`
- `get_principal_from_token(token, tenant_id)` → `(principal_id, tenant_dict)`
- Returns `None` for invalid tokens; raises `AdCPAuthenticationError` if `require_valid_token=True`
- Test mode (`ADCP_AUTH_TEST_MODE=true`): click "Log in to Dashboard", password `test123`

---

## 5. MCP Tools — Complete Reference

All 16 tools are defined in `src/core/tools/` and registered in `src/core/main.py`.

### 5.1 `get_products`
**File:** [src/core/tools/products.py](../src/core/tools/products.py)

Fetches the tenant's product catalogue. Accepts an optional `brief` string; an AI ranking agent (`src/services/ai/agents/ranking_agent.py`) scores and re-orders products by relevance.

**What it does:**
- Loads all active `Product` DB records for the tenant
- Converts via `convert_product_model_to_schema()`
- If `brief` given, re-ranks using `RankingAgent`
- Returns `GetProductsResponse` with list of `ProductCard`

**What it does NOT do:** It does not create products — that is an admin UI operation.

---

### 5.2 `create_media_buy`
**File:** [src/core/tools/media_buy_create.py](../src/core/tools/media_buy_create.py)

The core campaign creation tool. Validates the request, optionally runs policy checks and AI review, then delegates to the adapter.

**Flow:**
1. Parse `CreateMediaBuyRequest` (product_ids, packages, targeting, budget, dates)
2. Load matching `Product` records
3. Run `validate_media_buy_request()` on the adapter
4. If tenant has HITL enabled → create `WorkflowStep` (pending approval)
5. Otherwise → call `adapter.create_media_buy()`
6. Persist `MediaBuy` + `MediaPackage` rows
7. Return `CreateMediaBuyResponse` (success or error)

**Supported pricing models per adapter:** see §6.

---

### 5.3 `update_media_buy`
**File:** [src/core/tools/media_buy_update.py](../src/core/tools/media_buy_update.py)

Updates an existing campaign's packages, targeting, budget, or dates. Calls `adapter.update_media_buy()`.

---

### 5.4 `get_media_buy_delivery`
**File:** [src/core/tools/media_buy_delivery.py](../src/core/tools/media_buy_delivery.py)

Returns delivery metrics for a campaign. Delegates to `adapter.get_media_buy_delivery()` and formats as `GetMediaBuyDeliveryResponse`.

Supports date-range filtering, per-placement breakdown, and daily time-series data.

---

### 5.5 `get_media_buys`
**File:** [src/core/tools/media_buy_list.py](../src/core/tools/media_buy_list.py)

Lists campaigns for the current principal/tenant with pagination support. Returns `GetMediaBuysResponse`.

---

### 5.6 `sync_creatives`
**File:** [src/core/tools/creatives/__init__.py](../src/core/tools/creatives/__init__.py)

Uploads creative assets to the ad server. Calls `adapter.add_creative_assets()`, then `adapter.associate_creatives()` to link creatives to line items.

---

### 5.7 `list_creatives`
**File:** [src/core/tools/creatives/listing.py](../src/core/tools/creatives/listing.py)

Lists creatives for the principal with optional filters. Returns `ListCreativesResponse`.

---

### 5.8 `list_creative_formats`
**File:** [src/core/tools/creative_formats.py](../src/core/tools/creative_formats.py)

Returns the creative formats supported by the tenant's active adapter. Delegates to `adapter.get_creative_formats()`.

---

### 5.9 `list_authorized_properties`
**File:** [src/core/tools/properties.py](../src/core/tools/properties.py)

Returns the ad inventory properties (publishers/placements) the principal is authorised to buy against. Reads from `AuthorizedProperty` DB records.

---

### 5.10 `list_accounts`
**File:** [src/core/tools/accounts.py](../src/core/tools/accounts.py)

Lists advertiser accounts accessible to the principal. Returns `ListAccountsResponse`.

---

### 5.11 `sync_accounts`
**File:** [src/core/tools/accounts.py](../src/core/tools/accounts.py)

Syncs accounts from the connected ad server into the local database.

---

### 5.12 `get_adcp_capabilities`
**File:** [src/core/tools/capabilities.py](../src/core/tools/capabilities.py)

Returns the full capability manifest for the tenant's adapter — supported pricing models, targeting types, creative formats. AI agents call this to understand what they can request.

---

### 5.13 `update_performance_index`
**File:** [src/core/tools/performance.py](../src/core/tools/performance.py)

Updates per-format performance scores used to rank products in `get_products`. Writes to `FormatPerformanceMetrics`.

---

### 5.14 `list_tasks`
**File:** [src/core/tools/task_management.py](../src/core/tools/task_management.py)

Lists pending `WorkflowStep` records assigned to (or visible to) the principal. Used by AI agents in a HITL loop.

---

### 5.15 `get_task`
**File:** [src/core/tools/task_management.py](../src/core/tools/task_management.py)

Fetches a single `WorkflowStep` by ID, including its payload.

---

### 5.16 `complete_task`
**File:** [src/core/tools/task_management.py](../src/core/tools/task_management.py)

Marks a `WorkflowStep` as approved or rejected. If approved, triggers the deferred adapter call.

---

## 6. Adapter Architecture

### 6.1 Abstract base — `src/adapters/base.py`

Every adapter extends `AdServerAdapter(ABC)`. Key abstract methods:

| Method | Signature | Purpose |
|--------|-----------|---------|
| `create_media_buy` | `(request, packages, start_time, end_time)` → `CreateMediaBuyResponse` | Create campaign + line items |
| `add_creative_assets` | `(media_buy_id, assets)` → `list[AssetStatus]` | Upload creative files |
| `associate_creatives` | `(line_item_ids, creative_ids)` → `list[dict]` | Link creatives to line items |
| `check_media_buy_status` | `(media_buy_id, today)` → `CheckMediaBuyStatusResponse` | Poll campaign status |
| `get_media_buy_delivery` | `(media_buy_id, date_range, today)` → `AdapterGetMediaBuyDeliveryResponse` | Fetch delivery metrics |

Optional (have default implementations):

| Method | Default | Purpose |
|--------|---------|---------|
| `get_supported_pricing_models()` | `{"cpm"}` | Declare supported pricing |
| `get_targeting_capabilities()` | all `False` | Declare geo/metro/postal support |
| `validate_media_buy_request()` | `[]` (no errors) | Pre-flight validation |
| `get_packages_snapshot()` | `[]` | Real-time impression data |
| `update_media_buy_performance_index()` | no-op | Update performance scores |
| `update_media_buy()` | not implemented | Update existing campaign |
| `get_creative_formats()` | `[]` | List supported creative formats |
| `get_available_inventory()` | `{}` | Discover inventory |

### 6.2 `AdapterCapabilities` dataclass

Declared at class level; drives UI feature flags:

```python
@dataclass
class AdapterCapabilities:
    supports_inventory_sync:      bool
    supports_inventory_profiles:  bool
    supports_custom_targeting:    bool
    supports_geo_targeting:       bool
    supports_dynamic_products:    bool
    supported_pricing_models:     list[str] | None
    supports_webhooks:            bool
    supports_realtime_reporting:  bool
```

### 6.3 `TargetingCapabilities` dataclass

Describes geographic precision. Fields (all `bool`, default `False`):

- `geo_countries`, `geo_regions`
- Metro: `nielsen_dma`, `eurostat_nuts2`, `uk_itl1`, `uk_itl2`
- Postal: `us_zip`, `us_zip_plus_four`, `ca_fsa`, `ca_full`, `gb_outward`, `gb_full`, `de_plz`, `fr_code_postal`, `au_postcode`

### 6.4 Available adapters

| Adapter | File | Pricing Models | Key Features |
|---------|------|---------------|--------------|
| **Mock** | `mock_ad_server.py` | All (CPM, VCPM, CPCV, CPP, CPC, CPV, FLAT_RATE) | Testing; stateless; no external dependencies |
| **Google Ad Manager (GAM)** | `google_ad_manager.py` + `gam/` | CPM, VCPM, CPC, FLAT_RATE | Most complete; inventory discovery; GAM API v13 |
| **Kevel** | `kevel.py` | CPM, CPC | Contextual/DSP; REST API |
| **Triton Digital** | `triton_digital.py` | CPM | Audio/podcast advertising |
| **Xandr** | `xandr.py` | CPM | Programmatic DSP |
| **Broadstreet** | `broadstreet/` | CPM | Display platform |

#### GAM specifics (most complex adapter)

- **Auth:** OAuth2 service account or user credentials (`gam/auth.py`)
- **Line item type selection:** Automatic based on pricing model + guarantee flag
  - `FLAT_RATE` → `SPONSORSHIP` with CPD conversion
  - `VCPM` → `STANDARD` only
- **Inventory discovery:** `gam_inventory_discovery.py` scans ad units
- **Reporting:** `gam_reporting_api.py` / `gam_reporting_service.py` for delivery data
- **Config schema:** `gam_implementation_config_schema.py`

### 6.5 Adapter selection

At runtime, `SELECTED_ADAPTER` is determined from `config["ad_server"]["adapter"]`. Available values: `"mock"`, `"gam"`, `"kevel"`, `"triton"`, `"triton_digital"`. Invalid values fall back to `"mock"`.

---

## 7. Database Layer

### PostgreSQL only — no SQLite support

All queries use **SQLAlchemy 2.0** syntax (`select()` + `scalars()`). The `session.query()` API is banned by pre-commit hook.

### Core models — `src/core/database/models.py`

| Model | Table | Key Fields | Notes |
|-------|-------|-----------|-------|
| `Tenant` | `tenants` | tenant_id, subdomain, name, policy_* | Multi-tenant root |
| `Principal` | `principals` | principal_id, tenant_id, email, roles, permissions | User/API key identity |
| `User` | `users` | user_id, tenant_id, email | Human login accounts |
| `Product` | `products` | product_id, tenant_id, name, pricing_options, format_ids | Sellable packages |
| `PricingOption` | `pricing_options` | product_id, pricing_model, rate, currency | Per-product pricing |
| `CurrencyLimit` | `currency_limits` | tenant_id, currency, max_budget | Budget guard rails |
| `MediaBuy` | `media_buys` | media_buy_id, tenant_id, principal_id, status | Campaign record |
| `MediaPackage` | `media_packages` | package_id, media_buy_id, targeting, budget | Line item record |
| `Creative` | `creatives` | creative_id, tenant_id, status, format_id | Asset record |
| `CreativeReview` | `creative_reviews` | creative_id, status, confidence | AI review result |
| `CreativeAssignment` | `creative_assignments` | creative_id, package_id | Creative↔line item link |
| `Account` | `accounts` | account_id, tenant_id, name, billing_info | Advertiser accounts |
| `AdapterConfig` | `adapter_configs` | tenant_id, adapter_type, connection_config | Adapter credentials |
| `TenantAuthConfig` | `tenant_auth_configs` | tenant_id, oidc_* | SSO/OIDC settings |
| `WorkflowStep` | `workflow_steps` | step_id, action, status, payload | HITL approval queue |
| `ObjectWorkflowMapping` | `object_workflow_mappings` | object_id, step_id | Object↔workflow link |
| `Strategy` | `strategies` | strategy_id, tenant_id, config | Pricing strategies |
| `StrategyState` | `strategy_states` | strategy_id, state | Strategy runtime state |
| `AuthorizedProperty` | `authorized_properties` | property_id, tenant_id, name | Inventory permissions |
| `PropertyTag` | `property_tags` | tenant_id, tag, property_ids | Tag-based inventory groups |
| `GAMInventory` | `gam_inventory` | ad_unit_id, tenant_id, name | GAM ad unit cache |
| `InventoryProfile` | `inventory_profiles` | profile_id, tenant_id, filters | Saved inventory queries |
| `GAMOrder` / `GAMLineItem` | `gam_orders` / `gam_line_items` | gam_order_id, tenant_id, status | GAM order cache |
| `SyncJob` | `sync_jobs` | job_id, type, status, tenant_id | Background sync tracking |
| `AuditLog` | `audit_logs` | action, actor_id, tenant_id, payload | Immutable activity log |
| `PushNotificationConfig` | `push_notification_configs` | tenant_id, url, auth | Webhook endpoints |
| `WebhookDeliveryRecord` | `webhook_delivery_records` | delivery_id, status | Webhook dispatch record |
| `Context` | `contexts` | context_id, tenant_id | Request context store |
| `FormatPerformanceMetrics` | `format_performance_metrics` | format_id, tenant_id, score | Product ranking data |
| `CreativeAgent` | `creative_agents` | agent_id, tenant_id, config | AI creative reviewer config |
| `SignalsAgent` | `signals_agents` | agent_id, tenant_id, config | AXE signals agent config |
| `PublisherPartner` | `publisher_partners` | partner_id, tenant_id, domain | Publisher access grants |
| `TenantManagementConfig` | `tenant_management_configs` | tenant_id, super_admin | Platform management |

### JSON columns

All JSON fields use `JSONType` (not plain `JSON`). Never pass `json.dumps()` to these columns — the type handles serialisation.

```python
from src.core.database.json_type import JSONType
config: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
```

### Migrations — `alembic/versions/`

- Create: `uv run alembic revision -m "description"`
- Apply: `uv run python scripts/ops/migrate.py`
- Applied automatically on server startup
- Never modify a migration after it has been committed

---

## 8. Repository Pattern

All database reads and writes go through repository classes in `src/core/database/repositories/`.

| Repository | File | Manages |
|-----------|------|---------|
| `AccountRepository` | `account.py` | Account CRUD |
| `AdapterConfigRepository` | `adapter_config.py` | Adapter credentials |
| `CreativeRepository` | `creative.py` | Creative assets |
| `CurrencyLimitRepository` | `currency_limit.py` | Budget limits |
| `DeliveryRepository` | `delivery.py` | Delivery records |
| `MediaBuyRepository` | `media_buy.py` | Campaign records |
| `ProductRepository` | `product.py` | Product catalogue |
| `TenantConfigRepository` | `tenant_config.py` | Tenant settings |
| `WorkflowRepository` | `workflow.py` | Workflow steps |
| `UnitOfWork` | `uow.py` | Transaction manager |

### Repository interface pattern

```python
class MediaBuyRepository:
    def __init__(self, session: Session): ...
    def get_by_id(self, media_buy_id: str, tenant_id: str) -> MediaBuy | None: ...
    def list_for_tenant(self, tenant_id: str, ...) -> list[MediaBuy]: ...
    def create_from_request(self, req, identity) -> MediaBuy: ...
```

**`_impl` functions never call `get_db_session()` directly.** That belongs in the repository layer.

---

## 9. Schema Layer (Pydantic)

Schemas live in `src/core/schemas/`. All client-facing models extend AdCP library types.

### Inheritance rule

```python
from adcp.types import Product as LibraryProduct

class Product(LibraryProduct):
    implementation_config: dict[str, Any] | None = Field(default=None, exclude=True)
```

Never copy parent fields — extend with inheritance. Use `Library*` alias for imports.

### Key schema groups

#### Campaign management
- `CreateMediaBuyRequest` / `CreateMediaBuyResponse`
- `CreateMediaBuySuccess` / `CreateMediaBuyError`
- `CreateMediaBuyResult` — unified wrapper
- `UpdateMediaBuyRequest` / `UpdateMediaBuyResponse`
- `CheckMediaBuyStatusResponse`

#### Products
- `Product` (extends `LibraryProduct`)
- `ProductCard` / `ProductCardDetailed` — UI representations
- `GetProductsRequest` / `GetProductsResponse`
- `ProductFilters`, `ProductCatalog`, `ProductPerformance`

#### Creatives
- `Creative` (extends `LibraryCreative`)
- `CreativeStatusEnum`, `CreativeApproval`, `CreativeAssignment`, `CreativeAdaptation`
- `SyncCreativesRequest/Response`, `ListCreativesRequest/Response`
- `AddCreativeAssetsRequest/Response`, `CheckCreativeStatusRequest/Response`

#### Delivery & reporting
- `GetMediaBuyDeliveryRequest/Response`
- `DeliveryTotals`, `AggregatedTotals`, `PlacementBreakdown`
- `PackageDelivery`, `DailyBreakdown`, `MediaBuyDeliveryData`
- `ReportingPeriod`, `DeliveryType` enum

#### Targeting
- `Targeting` / `TargetingOverlay`
- `TargetingCapability`, `ChannelTargetingCapabilities`
- `GetTargetingCapabilitiesResponse`

#### Core primitives
- `Principal`, `Package`, `PackageRequest`
- `Budget`, `Measurement`, `DeliveryMeasurement`
- `FrequencyCap`, `Signal`, `SignalFilters`
- `FormatId`, `Format`, `FormatReference`
- `MediaPackage`, `PackagePerformance`, `Snapshot`
- `ReportingPeriod`

#### Workflow / HITL
- `HumanTask`, `CreateHumanTaskRequest`
- `GetPendingTasksResponse`, `VerifyTaskResponse`
- `TaskStatus` enum: `pending`, `approved`, `rejected`, `cancelled`

#### Accounts & inventory
- `AccountReference`
- `ListAuthorizedPropertiesResponse`

### Nested serialisation

Parent models override `model_dump()` when they contain nested custom models:

```python
def model_dump(self, **kwargs):
    result = super().model_dump(**kwargs)
    if "creatives" in result and self.creatives:
        result["creatives"] = [c.model_dump(**kwargs) for c in self.creatives]
    return result
```

This is required because Pydantic does not auto-call custom `model_dump()` on nested models.

### Validation mode

- **`ENVIRONMENT=production`** → `extra="ignore"` (forward-compatible; ignores unknown fields)
- **All other environments** → `extra="forbid"` (strict; fails on unknown fields)

---

## 10. Services Layer

### `src/services/ai/agents/`

| Agent | File | Purpose |
|-------|------|---------|
| `RankingAgent` | `ranking_agent.py` | Scores products against a campaign brief |
| `NamingAgent` | `naming_agent.py` | Generates names for orders/line items |
| `PolicyAgent` | `policy_agent.py` | Reviews campaign for advertising policy compliance |
| `ReviewAgent` | `review_agent.py` | Reviews creative assets for brand safety |

All agents use **pydantic-ai** (`pydantic-ai>=0.3.0`) with Gemini as the underlying model.

### Platform-specific services

| Service | Purpose |
|---------|---------|
| `gam_inventory_service.py` | Discovers and caches GAM ad units |
| `gam_orders_service.py` | Syncs GAM orders into local DB |
| `gam_product_config_service.py` | Manages GAM-specific product configuration |
| `gam_reporting_api.py` | Calls GAM reporting API |
| `gam_reporting_service.py` | Processes and aggregates GAM reports |

### Other services

| Service | Purpose |
|---------|---------|
| `order_approval_service.py` | Processes approved workflow steps → calls adapter |
| `background_approval_service.py` | Async approval processing loop |
| `background_sync_service.py` | Background data sync with ad servers |
| `policy_check_service.py` | Validates campaigns against tenant policies |
| `policy_service.py` | Manages policy configuration |
| `property_discovery_service.py` | Discovers new inventory properties |
| `property_verification_service.py` | Validates property existence |
| `targeting_capabilities.py` | Builds targeting capability matrix |
| `dynamic_pricing_service.py` | Computes dynamic prices from strategies |
| `dynamic_products.py` | Generates products dynamically from inventory |
| `default_products.py` | Generates default product catalogue for new tenants |
| `setup_checklist_service.py` | Tracks tenant onboarding completeness |
| `activity_feed.py` | Records user-visible activity events |
| `delivery_simulator.py` | Simulates delivery for mock adapter |
| `format_metrics_service.py` | Aggregates format performance data |
| `webhook_delivery_service.py` | HTTP delivery of webhook payloads |
| `protocol_webhook_service.py` | AdCP-protocol-level webhooks |
| `webhook_verification.py` | Validates webhook signatures |
| `slack_notifier.py` | Slack notifications for events |
| `auth_config_service.py` | OIDC configuration management |
| `gcp_service_account_service.py` | GCP service account management |
| `ai_parsing_comparison.py` | A/B comparison of AI parsing approaches |
| `targeting_dimensions.py` | Geo/targeting dimension helpers |

---

## 11. Admin UI

Built on **Flask** with Google OAuth and optional OIDC/SSO.

### Access
- Local: `http://localhost:8000/admin/` or `/tenant/<subdomain>`
- Authentication: Google OAuth (test mode: click "Log in to Dashboard", password `test123`)

### Blueprints — `src/admin/blueprints/`

| Blueprint | Purpose |
|-----------|---------|
| `accounts.py` | Advertiser account management |
| `adapters.py` | Adapter connection configuration |
| `activity_stream.py` | Activity feed viewer |
| `api.py` | Admin REST API |
| `auth.py` | Login / logout |
| `authorized_properties.py` | Inventory access grants |
| `creative_agents.py` | AI creative reviewer configuration |
| `creatives.py` | Creative asset management |
| `format_search.py` | Creative format browser |
| `gam.py` | GAM-specific configuration UI |
| `inventory.py` | Inventory management |
| `inventory_profiles.py` | Saved inventory filter sets |
| `oidc.py` | OIDC SSO configuration |
| `operations.py` | Business operations dashboard |
| `policy.py` | Advertising policy management |
| `principals.py` | API key / principal management |
| `products.py` | Product catalogue management |
| `publisher_partners.py` | Publisher partner management |
| `public.py` | Public-facing pages |
| `schemas.py` | Schema browser / validation |
| `settings.py` | Tenant settings |
| `signals_agents.py` | AXE signals agent configuration |
| `tenants.py` | Multi-tenant management (super admin) |
| `users.py` | User account management |
| `workflows.py` | HITL workflow dashboard |
| `core.py` | Shared admin utilities |

### Services used by admin

| Service | Purpose |
|---------|---------|
| `business_activity_service.py` | Activity event recording |
| `dashboard_service.py` | Dashboard metric aggregation |
| `media_buy_readiness_service.py` | Campaign readiness checklist |

---

## 12. Multi-Tenancy

### Tenant isolation

Every database model carries `tenant_id`. All repository queries filter by `tenant_id` — the `WorkflowRepository` additionally joins on `DBContext` for scoped queries (enforced by structural guard `test_architecture_workflow_tenant_isolation.py`).

### Tenant setup dependencies (order matters)

```
Tenant
  └── CurrencyLimit (USD required for budget validation)
  └── PropertyTag ("all_inventory" required for property_tags references)
  └── Products (require both CurrencyLimit and PropertyTag)
```

### Tenant resolution at runtime

See §4. The `TenantContext` wrapper provides typed access to tenant settings including:
- Policy configuration (`policy_*` fields)
- Measurement provider settings
- Adapter selection
- Auth configuration
- Gemini API key (encrypted at rest)

---

## 13. Workflow / HITL System

### Purpose

Human-in-the-Loop approval for campaign actions. When a tenant requires approval, the system creates a `WorkflowStep` instead of immediately calling the adapter.

### `WorkflowStep` model

```
status:  pending → approved | rejected | cancelled
action:  create_media_buy | update_media_buy | add_creative_assets
payload: serialised request data
assignee: principal_id or null (broadcast)
```

### Flow

1. `create_media_buy` tool detects HITL policy on tenant
2. Creates `WorkflowStep(action="create_media_buy", payload=<request>)`
3. Creates `ObjectWorkflowMapping(object_id=media_buy_id, step_id=...)`
4. Returns `CreateMediaBuySuccess` with `workflow_step_id` populated
5. Human approves via admin UI (`/admin/workflows`) or AI calls `complete_task`
6. `order_approval_service.py` dequeues approved steps and calls the adapter

### AI-driven HITL

AI agents poll `list_tasks`, then call `complete_task` with `approved: true/false` to approve/reject without human involvement. This enables fully automated workflows where the AI acts as the approver.

---

## 14. AI Agents

### Ranking Agent

**Input:** Product list + campaign brief string  
**Output:** Re-ordered product list with scores  
**Model:** Gemini (via pydantic-ai)  
**Used by:** `get_products` tool

### Naming Agent

**Input:** Campaign metadata (product, account, dates)  
**Output:** Generated order and line item names  
**Used by:** `create_media_buy` during adapter calls

### Policy Agent

**Input:** `CreateMediaBuyRequest`  
**Output:** Policy check result (pass/fail + reasons)  
**Used by:** `create_media_buy` when `policy_check_enabled` on tenant

### Review Agent

**Input:** Creative asset + brand guidelines  
**Output:** Approval decision + confidence score + rejection reasons  
**Used by:** `sync_creatives` when creative AI review is enabled

### Signals Agent (AXE)

Tenant-configured agent for audience signal enrichment. Config stored in `SignalsAgent` model. Used to inject contextual signals into targeting.

---

## 15. Background Schedulers

Both schedulers are started/stopped in the FastMCP `lifespan_context`.

### Delivery Webhook Scheduler — `src/services/delivery_webhook_scheduler.py`

- Runs on an async loop
- Polls `WebhookDeliveryRecord` for pending dispatches
- Calls `webhook_delivery_service.py` to HTTP-POST payloads
- Retries with backoff on failure
- Records results in `WebhookDeliveryLog`

### Media Buy Status Scheduler — `src/services/media_buy_status_scheduler.py`

- Polls the ad server for status changes on active campaigns
- Calls `adapter.check_media_buy_status()` for each active `MediaBuy`
- Updates `MediaBuy.status` in DB
- Triggers status-change webhooks if configured

---

## 16. Testing Infrastructure

### Test runner: tox + tox-uv

```bash
make quality                  # Format + lint + typecheck + unit tests (before every commit)
./run_all_tests.sh            # Full suite: Docker + all 5 envs via tox -p
tox -e unit                   # Unit tests only (no Docker)
tox -e integration            # Integration (needs Postgres)
tox -e e2e                    # End-to-end (full Docker stack)
tox -e admin                  # Admin UI tests (full Docker stack)
tox -e bdd                    # BDD behavioral tests
```

### Test environments

| Directory | Runner env | Infrastructure needed |
|-----------|-----------|----------------------|
| `tests/unit/` | `tox -e unit` | None |
| `tests/integration/` | `tox -e integration` | PostgreSQL |
| `tests/e2e/` | `tox -e e2e` | Full Docker stack |
| `tests/admin/` | `tox -e admin` | Full Docker stack |
| `tests/bdd/` | `tox -e bdd` | PostgreSQL |

### Test factories — `tests/factories/`

All integration test data created via factory-boy factories, never inline `session.add()`:

```python
from tests.factories import TenantFactory, MediaBuyFactory

tenant = TenantFactory.create_sync()
media_buy = MediaBuyFactory.create_sync(tenant_id=tenant.tenant_id)
```

### Structural guards (automated architecture enforcement)

19 AST-scanning guards run on every `make quality`. A new violation = build fails immediately. Key guards:

| Guard | What it prevents |
|-------|-----------------|
| `test_no_toolerror_in_impl.py` | `ToolError` in `_impl` functions |
| `test_transport_agnostic_impl.py` | Transport imports in `_impl` |
| `test_impl_resolved_identity.py` | `Context` in `_impl` signatures |
| `test_architecture_schema_inheritance.py` | Duplicate schema fields |
| `test_architecture_boundary_completeness.py` | Dropped `_impl` parameters in wrappers |
| `test_architecture_repository_pattern.py` | Inline `get_db_session()` in business logic |
| `test_architecture_no_raw_select.py` | Raw `select()` outside repositories |
| `test_architecture_migration_completeness.py` | Empty `upgrade()`/`downgrade()` |
| `test_architecture_single_migration_head.py` | Branched migration graph |
| `test_architecture_obligation_coverage.py` | Obligation without a test |

### Pre-commit hooks (11 active)

Catch on every commit:
- Route conflicts (`check_route_conflicts.py`)
- SQLAlchemy 1.x patterns
- Star imports
- Excessive mocks in tests (>10 per file)
- Documentation link breakage
- Import usage issues
- Code duplication (ratcheting baseline in `.duplication-baseline`)

---

## 17. Configuration & Environment

### Required secrets — `.env.secrets`

```
GEMINI_API_KEY              AI agent model
GOOGLE_CLIENT_ID            Admin UI OAuth
GOOGLE_CLIENT_SECRET        Admin UI OAuth
SUPER_ADMIN_EMAILS          Comma-separated super-admin emails
GAM_OAUTH_CLIENT_ID         GAM user credential OAuth
GAM_OAUTH_CLIENT_SECRET     GAM user credential OAuth
APPROXIMATED_API_KEY        Virtual host service (optional)
```

### Key environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | (required) | PostgreSQL connection string |
| `ENVIRONMENT` | `development` | `production` → relaxed schema validation |
| `ADCP_AUTH_TEST_MODE` | `true` (local) | Enables test login with password |
| `DB_ECHO` | `false` | Log all SQL queries |
| `LOGFIRE_TOKEN` | (optional) | Logfire observability |

### Database schema setup order

1. Tenant (subdomain, name)
2. CurrencyLimit (USD minimum)
3. PropertyTag (at least "all_inventory")
4. Products (need both above)
5. Principals / API keys
6. AdapterConfig

---

## 18. What Is and Is Not Possible

### What you CAN do through MCP tools (as an AI agent)

| Action | Tool |
|--------|------|
| Discover what products are available | `get_products` |
| Create a campaign with targeting and budget | `create_media_buy` |
| Update an existing campaign | `update_media_buy` |
| Check delivery metrics | `get_media_buy_delivery` |
| List all campaigns | `get_media_buys` |
| Upload creative assets | `sync_creatives` |
| See what creatives exist | `list_creatives` |
| Know what creative formats the platform supports | `list_creative_formats` |
| Know what inventory is available | `list_authorized_properties` |
| Know what the platform supports (pricing, targeting) | `get_adcp_capabilities` |
| See advertiser accounts | `list_accounts` |
| Sync accounts from ad server | `sync_accounts` |
| Work with HITL approval tasks | `list_tasks`, `get_task`, `complete_task` |
| Update product performance rankings | `update_performance_index` |

### What you CANNOT do through MCP tools

| Action | Why | Where it lives instead |
|--------|-----|----------------------|
| Create or delete products | Admin-only operation | Admin UI → Products |
| Configure adapter credentials | Admin-only | Admin UI → Adapters |
| Manage tenants | Super-admin only | Admin UI → Tenants |
| Manage principals / API keys | Admin-only | Admin UI → Principals |
| Configure OIDC / SSO | Admin-only | Admin UI → Settings |
| View audit logs | Admin-only | Admin UI → Activity Stream |
| Configure push notification webhooks | Admin-only | Admin UI → Settings |
| Configure AI review policies | Admin-only | Admin UI → Policy |
| Run migrations | Ops script | `scripts/ops/migrate.py` |
| Create a new tenant | Super-admin only | Admin UI → Tenants |

### Targeting support by adapter

| Targeting Type | Mock | GAM | Kevel | Triton | Xandr |
|----------------|------|-----|-------|--------|-------|
| Geo countries | Yes | Yes | Yes | No | Yes |
| Geo regions | Yes | Yes | No | No | Yes |
| Nielsen DMA | No | Yes | No | No | Yes |
| Eurostat NUTS2 | No | Yes | No | No | No |
| UK ITL1/ITL2 | No | Yes | No | No | No |
| US ZIP codes | No | Yes | No | No | Yes |
| UK postcodes | No | Yes | No | No | No |
| Custom targeting | No | Yes | No | No | No |

### Pricing model support by adapter

| Model | Mock | GAM | Kevel | Triton | Xandr |
|-------|------|-----|-------|--------|-------|
| CPM | Yes | Yes | Yes | Yes | Yes |
| VCPM | Yes | Yes | No | No | No |
| CPC | Yes | Yes | Yes | No | Yes |
| CPCV | Yes | No | No | No | No |
| CPP | Yes | No | No | No | No |
| CPV | Yes | No | No | No | No |
| FLAT_RATE | Yes | Yes | No | No | No |

### Schema validation

- Unknown extra fields are **silently ignored in production** (`extra="ignore"`)
- Unknown extra fields **cause validation errors in development/CI** (`extra="forbid"`)
- This is intentional for forward compatibility

---

## 19. Adding New Features — Decision Trees

### Add a new MCP tool

1. Create `src/core/tools/<tool_name>.py` with `async def <tool_name>(ctx: Context, ...) -> Response`
2. Implement `async def _<tool_name>_impl(req, identity: ResolvedIdentity) -> Result`
3. The `_impl` function: no transport imports, accepts `ResolvedIdentity`, raises `AdCPError`
4. In `src/core/main.py`: import the function, register with `mcp.tool()(with_error_logging(fn))`
5. Add A2A raw function in `src/core/tools.py` (same `_impl`, different wrapper)
6. Add REST route in `src/routes/api_v1.py` if needed
7. Write unit test in `tests/unit/test_<tool_name>.py`
8. Run `make quality`

### Add a new adapter

1. Create `src/adapters/<name>.py` extending `AdServerAdapter`
2. Implement all abstract methods
3. Set `capabilities: AdapterCapabilities` at class level
4. Override `get_supported_pricing_models()` and `get_targeting_capabilities()` as needed
5. Add `"<name>"` to `AVAILABLE_ADAPTERS` list in `src/core/main.py`
6. Add connection config class extending `BaseConnectionConfig`
7. Write tests in `tests/unit/adapters/test_<name>.py`

### Add a new database model

1. Add class to `src/core/database/models.py` extending `Base`
2. Use SQLAlchemy 2.0 `Mapped[]` annotations
3. Use `JSONType` for any JSON columns
4. Create `src/core/database/repositories/<name>.py` with the repository class
5. Create Alembic migration: `uv run alembic revision -m "add <name> table"`
6. Write integration test using factories, not `session.add()`
7. Run `make quality` + `tox -e integration`

### Add a new admin UI page

1. Create `src/admin/blueprints/<resource>.py` as a Flask blueprint
2. Register in `src/admin/app.py`
3. Add template in `templates/admin/`
4. Use `url_for()` for all URLs (never hardcode)
5. Use `request.script_root` in any JavaScript for API calls
6. Check for route conflicts: `grep -r "@.*route.*your/path" src/`

### Modify a schema

1. Check if the field exists in the adcp library type first
2. If yes: inherit, do not duplicate
3. If it's an internal field: add with `exclude=True`
4. Run `pytest tests/unit/test_adcp_contract.py` to verify compliance
5. If nested models need custom serialisation, override `model_dump()`

---

*Last updated: based on codebase as of branch `develop`, commit `922d9771`.*
