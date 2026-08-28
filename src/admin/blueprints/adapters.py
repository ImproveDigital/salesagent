"""Adapters management blueprint."""

import logging

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import attributes

from src.adapters import get_adapter_schemas
from src.adapters.improvedigital._logging import safe_upstream_body_excerpt
from src.admin.utils import require_tenant_access
from src.admin.utils.audit_decorator import log_admin_action
from src.core.database.database_session import get_db_session
from src.core.database.models import AdapterConfig, Product

logger = logging.getLogger(__name__)

# Create blueprint
adapters_bp = Blueprint("adapters", __name__)


@adapters_bp.route("/adapters/mock/config/<tenant_id>/<product_id>", methods=["GET", "POST"])
@require_tenant_access()
def mock_config(tenant_id, product_id, **kwargs):
    """Configure mock adapter settings for a product."""
    with get_db_session() as session:
        stmt = select(Product).filter_by(tenant_id=tenant_id, product_id=product_id)
        product = session.scalars(stmt).first()

        if not product:
            flash("Product not found", "error")
            return redirect(url_for("products.list_products", tenant_id=tenant_id))

        if request.method == "POST":
            # Handle form submission to update mock config
            try:
                config = product.implementation_config or {}

                # Helper function to safely parse and validate numeric values
                def parse_int(field_name, default, min_val=None, max_val=None):
                    try:
                        value = int(request.form.get(field_name, default))
                        if min_val is not None and value < min_val:
                            raise ValueError(f"{field_name} must be at least {min_val}")
                        if max_val is not None and value > max_val:
                            raise ValueError(f"{field_name} must be at most {max_val}")
                        return value
                    except (ValueError, TypeError) as e:
                        raise ValueError(f"Invalid value for {field_name}: {e}")

                def parse_float(field_name, default, min_val=None, max_val=None):
                    try:
                        value = float(request.form.get(field_name, default))
                        if min_val is not None and value < min_val:
                            raise ValueError(f"{field_name} must be at least {min_val}")
                        if max_val is not None and value > max_val:
                            raise ValueError(f"{field_name} must be at most {max_val}")
                        return value
                    except (ValueError, TypeError) as e:
                        raise ValueError(f"Invalid value for {field_name}: {e}")

                # Traffic simulation (with validation)
                config["daily_impressions"] = parse_int("daily_impressions", 100000, min_val=0)
                config["fill_rate"] = parse_float("fill_rate", 85, min_val=0, max_val=100)
                config["ctr"] = parse_float("ctr", 0.5, min_val=0, max_val=100)
                config["viewability_rate"] = parse_float("viewability_rate", 70, min_val=0, max_val=100)

                # Performance simulation (with validation)
                config["latency_ms"] = parse_int("latency_ms", 50, min_val=0, max_val=60000)
                config["error_rate"] = parse_float("error_rate", 0.1, min_val=0, max_val=100)

                # Test scenarios (validated choices)
                test_mode = request.form.get("test_mode", "normal")
                valid_modes = ["normal", "high_demand", "degraded", "outage"]
                if test_mode not in valid_modes:
                    raise ValueError(f"Invalid test_mode: {test_mode}")
                config["test_mode"] = test_mode
                config["price_variance"] = parse_float("price_variance", 10, min_val=0, max_val=100)
                config["seasonal_factor"] = parse_float("seasonal_factor", 1.0, min_val=0.1, max_val=10.0)

                # Delivery simulation (with validation)
                config["delivery_simulation"] = {
                    "enabled": "delivery_simulation_enabled" in request.form,
                    "time_acceleration": parse_int("time_acceleration", 3600, min_val=1, max_val=86400),
                    "update_interval_seconds": parse_float("update_interval_seconds", 1.0, min_val=0.1, max_val=60),
                }

                # Note: Creative formats are managed in product.format_ids (via add/edit product page)
                # NOT in implementation_config - removing format handling to avoid duplication

                # Debug settings (boolean - safe)
                config["verbose_logging"] = "verbose_logging" in request.form
                config["predictable_ids"] = "predictable_ids" in request.form

                product.implementation_config = config
                attributes.flag_modified(product, "implementation_config")
                session.commit()

                flash("Mock adapter configuration saved successfully!", "success")
                return redirect(url_for("adapters.mock_config", tenant_id=tenant_id, product_id=product_id))
            except ValueError as e:
                logger.warning(f"Validation error in mock config: {e}")
                flash(f"Invalid configuration: {str(e)}", "error")
            except Exception as e:
                logger.error(f"Error saving mock config: {e}", exc_info=True)
                flash(f"Error saving configuration: {str(e)}", "error")

        # GET request - render template with product config
        config = product.implementation_config or {}

        return render_template(
            "adapters/mock_product_config.html",
            tenant_id=tenant_id,
            product=product,
            config=config,
        )


def _secret_fields_for_connection_schema(connection_config: type[BaseModel] | None) -> list[str]:
    """Field names the connection schema marks ``secret`` via json_schema_extra."""
    if connection_config is None:
        return []
    return [
        name
        for name, field in connection_config.model_fields.items()
        if isinstance(field.json_schema_extra, dict) and field.json_schema_extra.get("secret")
    ]


def _preserve_omitted_secret_fields(config_data: dict, existing_config: dict, *, secret_fields: list[str]) -> None:
    """Carry stored secrets forward when the caller omits them (UX: leave the
    password field blank to keep the existing credential)."""
    for field_name in [f for f in secret_fields if not config_data.get(f)]:
        existing_value = existing_config.get(field_name)
        if existing_value:
            config_data[field_name] = existing_value


def _apply_secret_field_rules(tenant_id: str, connection_config, config_data: dict) -> str | None:
    """Enforce the two secret-field rules on a submitted adapter config:

    (a) reject any submitted ciphertext on the wire — a tenant admin must
        not be able to authenticate a session by replaying another tenant's
        leaked DB-row ciphertext (cross-tenant credential smuggling),
    (b) preserve the previously-stored value when the caller omits the
        field (UX: leave password blank to keep existing credential).

    Mutates ``config_data`` in place; returns an error message for (a),
    ``None`` otherwise.
    """
    from src.core.database.repositories.adapter_config import AdapterConfigRepository
    from src.core.utils.encryption import is_encrypted

    secret_fields = _secret_fields_for_connection_schema(connection_config)
    for field_name in secret_fields:
        submitted = config_data.get(field_name)
        if submitted and is_encrypted(submitted):
            return f"{field_name} must be plaintext (encrypted-token replay rejected)"
    if secret_fields and any(not config_data.get(f) for f in secret_fields):
        with get_db_session() as session:
            existing = AdapterConfigRepository(session, tenant_id).find_by_tenant()
            if existing and existing.config_json:
                _preserve_omitted_secret_fields(config_data, existing.config_json, secret_fields=secret_fields)
    return None


@adapters_bp.route("/adapter/<adapter_name>/inventory_schema", methods=["GET"])
@require_tenant_access()
def adapter_adapter_name_inventory_schema(tenant_id, **kwargs):
    """TODO: Extract implementation from admin_ui.py."""
    # Placeholder implementation
    return jsonify({"error": "Not yet implemented"}), 501


@adapters_bp.route("/setup_adapter", methods=["POST"])
@log_admin_action("setup_adapter")
@require_tenant_access()
def setup_adapter(tenant_id, **kwargs):
    """TODO: Extract implementation from admin_ui.py."""
    # Placeholder implementation
    return jsonify({"error": "Not yet implemented"}), 501


@adapters_bp.route("/api/tenant/<tenant_id>/adapter-config", methods=["POST"])
@log_admin_action("update_adapter_config")
@require_tenant_access()
def save_adapter_config(tenant_id, **kwargs):
    """Save adapter connection configuration.

    Validates config using Pydantic schema, then writes to both:
    - Legacy columns (for backwards compatibility)
    - config_json column (for schema-driven access)

    Request body:
    {
        "adapter_type": "mock" | "google_ad_manager" | etc,
        "config": { ... adapter-specific config ... }
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No JSON data provided"}), 400

        adapter_type = data.get("adapter_type")
        config_data = dict(data.get("config") or {})

        if not adapter_type:
            return jsonify({"success": False, "error": "adapter_type is required"}), 400

        # Secret-field rules (ciphertext-replay reject + preserve-when-omitted).
        schemas = get_adapter_schemas(adapter_type)
        if schemas and schemas.connection_config:
            secret_error = _apply_secret_field_rules(tenant_id, schemas.connection_config, config_data)
            if secret_error:
                return jsonify({"success": False, "error": secret_error}), 400

        # Validate config against adapter schema (keeps Pydantic model flowing)
        validated_config = None
        if schemas and schemas.connection_config:
            try:
                validated_config = schemas.connection_config(**config_data)
            except ValidationError as e:
                return jsonify({"success": False, "error": f"Validation error: {e}"}), 400

        # Use the validated model for DB write (JSONType + engine json_serializer
        # handle BaseModel serialization). Fall back to raw dict if no schema.
        config_value = validated_config if validated_config is not None else config_data

        with get_db_session() as session:
            stmt = select(AdapterConfig).filter_by(tenant_id=tenant_id)
            adapter_config = session.scalars(stmt).first()

            if not adapter_config:
                adapter_config = AdapterConfig(
                    tenant_id=tenant_id,
                    adapter_type=adapter_type,
                    config_json=config_value,
                )
                session.add(adapter_config)
            else:
                adapter_config.adapter_type = adapter_type
                adapter_config.config_json = config_value
                attributes.flag_modified(adapter_config, "config_json")

            # Write to legacy columns for backwards compatibility
            if adapter_type == "mock" and validated_config is not None:
                adapter_config.mock_dry_run = getattr(validated_config, "dry_run", False)
                adapter_config.mock_manual_approval_required = getattr(
                    validated_config, "manual_approval_required", False
                )
            # Note: GAM, Kevel, Triton will be added as their schemas are created

            session.commit()
            logger.info(f"Saved adapter config for tenant {tenant_id}: {adapter_type}")

        return jsonify({"success": True, "adapter_type": adapter_type})

    except Exception as e:
        logger.error(f"Error saving adapter config: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@adapters_bp.route("/api/adapters/<adapter_type>/capabilities", methods=["GET"])
@require_tenant_access()
def get_adapter_capabilities(adapter_type, tenant_id, **kwargs):
    """Get capabilities for an adapter type.

    Returns the AdapterCapabilities for UI to show/hide sections.
    """
    from dataclasses import asdict

    schemas = get_adapter_schemas(adapter_type)
    if not schemas:
        return jsonify({"error": f"Unknown adapter type: {adapter_type}"}), 404

    if schemas.capabilities:
        return jsonify(asdict(schemas.capabilities))
    else:
        return jsonify({})


# Broadstreet-specific endpoints


@adapters_bp.route("/api/tenant/<tenant_id>/adapters/broadstreet/test-connection", methods=["POST"])
@require_tenant_access()
def test_broadstreet_connection(tenant_id, **kwargs):
    """Test Broadstreet API connection with provided credentials."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No JSON data provided"}), 400

        network_id = data.get("network_id")
        api_key = data.get("api_key")

        if not network_id or not api_key:
            return jsonify({"success": False, "error": "network_id and api_key are required"}), 400

        # Test connection by fetching network info
        from src.adapters.broadstreet import BroadstreetClient

        client = BroadstreetClient(access_token=api_key, network_id=network_id)
        network_info = client.get_network()

        if network_info:
            return jsonify(
                {
                    "success": True,
                    "network_name": network_info.get("name", "Unknown"),
                    "network_id": network_id,
                }
            )
        else:
            return jsonify({"success": False, "error": "Could not retrieve network information"})

    except Exception as e:
        logger.error(f"Broadstreet connection test failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@adapters_bp.route("/api/tenant/<tenant_id>/adapters/broadstreet/zones", methods=["GET"])
@require_tenant_access()
def list_broadstreet_zones(tenant_id, **kwargs):
    """List available zones from Broadstreet for the tenant's configured account."""
    try:
        # Get adapter config for this tenant
        with get_db_session() as session:
            stmt = select(AdapterConfig).filter_by(tenant_id=tenant_id)
            adapter_config = session.scalars(stmt).first()

            if not adapter_config:
                return jsonify({"zones": [], "error": "No adapter configured"}), 200

            # Get Broadstreet credentials from config_json or legacy columns
            config = adapter_config.config_json or {}
            network_id = config.get("network_id") or getattr(adapter_config, "broadstreet_network_id", None)
            api_key = config.get("api_key") or getattr(adapter_config, "broadstreet_api_key", None)

            if not network_id or not api_key:
                return jsonify({"zones": [], "error": "Broadstreet not configured"}), 200

            # Fetch zones from Broadstreet
            from src.adapters.broadstreet import BroadstreetClient

            client = BroadstreetClient(access_token=api_key, network_id=network_id)
            zones = client.get_zones()

            return jsonify(
                {
                    "zones": [
                        {"id": str(zone.get("id")), "name": zone.get("name", f"Zone {zone.get('id')}")}
                        for zone in zones
                    ]
                }
            )

    except Exception as e:
        logger.error(f"Error fetching Broadstreet zones: {e}", exc_info=True)
        return jsonify({"zones": [], "error": str(e)}), 500


@adapters_bp.route("/api/tenant/<tenant_id>/adapters/<adapter_type>/check-permissions", methods=["POST"])
@require_tenant_access(api_mode=True)
def check_adapter_permissions(tenant_id, adapter_type, **kwargs):
    """Probe upstream API for permission gaps before they bite us in production.

    Instantiates the configured adapter and calls its ``check_permissions()``
    method, which probes every endpoint the adapter depends on with cheap
    GETs. Returns a structured report — operators see at-connect time which
    AdCP features will work vs which need additional upstream IAM grants.

    Read-only by design — every probe is a GET. Opts into the embedded-write
    gate accordingly. Adapter must be configured on the tenant; an unconfigured
    adapter returns 400.
    """
    from dataclasses import asdict

    from src.adapters import ADAPTER_REGISTRY
    from src.core.database.repositories.adapter_config import AdapterConfigRepository

    adapter_class = ADAPTER_REGISTRY.get(adapter_type.lower())
    if not adapter_class:
        return jsonify({"success": False, "error": f"Unknown adapter type: {adapter_type}"}), 404

    with get_db_session() as session:
        repo = AdapterConfigRepository(session, tenant_id)
        config_row = repo.find_by_tenant()
        if config_row is None or config_row.adapter_type != adapter_type.lower():
            return (
                jsonify(
                    {
                        "success": False,
                        "error": f"No {adapter_type} adapter configured for tenant {tenant_id}",
                    }
                ),
                400,
            )
        adapter_config = dict(config_row.config_json or {})

    # The probe only needs a minimal principal — no real advertiser-scoped
    # calls happen. A stub principal_id keeps the adapter constructor happy.
    from src.core.schemas import Principal

    stub_principal = Principal(
        principal_id="__permissions_probe__",
        name="permissions-probe",
        platform_mappings={adapter_type: {"advertiser_id": "0"}},
    )

    try:
        adapter = adapter_class(
            config=adapter_config,
            principal=stub_principal,
            dry_run=False,
            tenant_id=tenant_id,
        )
        report = adapter.check_permissions()
    except Exception as exc:
        logger.warning("Permissions probe failed for tenant=%s adapter=%s: %s", tenant_id, adapter_type, exc)
        return jsonify({"success": False, "error": f"Could not run probe: {exc}"}), 500

    return jsonify(
        {
            "success": True,
            "report": {
                "adapter": report.adapter,
                "tenant_id": report.tenant_id,
                "checked_at": report.checked_at.isoformat(),
                "fully_operational": report.fully_operational,
                "error": report.error,
                "checks": [asdict(c) for c in report.checks],
            },
        }
    )


# Improve Digital-specific endpoints


def _resolve_improvedigital_credentials(tenant_id: str, data: dict) -> tuple[dict | None, str | None]:
    """Resolve Improve Digital client credentials from a request body, falling
    back to the values stored on AdapterConfig.config_json.

    Submitted ciphertext on the secret field is rejected to prevent
    cross-tenant replay. Returns ``(client_kwargs, error_message)``.
    """
    from src.core.utils.encryption import is_encrypted

    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    api_base_url = data.get("api_base_url")

    if client_secret and is_encrypted(client_secret):
        return None, "client_secret must be plaintext (encrypted-token replay rejected)"

    if not (client_id and client_secret):
        from src.core.database.repositories.adapter_config import AdapterConfigRepository

        with get_db_session() as session:
            existing = AdapterConfigRepository(session, tenant_id).find_by_tenant()
            if existing and existing.config_json:
                from src.adapters.improvedigital import ImproveDigitalConnectionConfig

                try:
                    rehydrated = ImproveDigitalConnectionConfig.model_validate(existing.config_json)
                    client_id = client_id or rehydrated.client_id
                    client_secret = client_secret or rehydrated.client_secret
                    api_base_url = api_base_url or rehydrated.api_base_url
                except ValidationError:
                    pass

    if not (client_id and client_secret):
        return None, "client_id + client_secret are required (submit them or save the configuration first)"

    client_kwargs: dict = {"client_id": client_id, "client_secret": client_secret}
    if api_base_url:
        client_kwargs["base_url"] = api_base_url
    return client_kwargs, None


def _improvedigital_rows(payload, *keys: str) -> list:
    """Unwrap a 360Yield list envelope (e.g. ``{"buying_entity_offices": [...]}``)."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return payload[key]
        for value in payload.values():
            if isinstance(value, list):
                return value
    return []


def _improvedigital_buyer_options(payload) -> list[dict]:
    """Normalize 360Yield buyer rows into ``{id, name}`` picker options.

    Buyers surface as a ``buyers`` list on ``/lookup/v1/user-details`` and on
    buying-entity office rows. ``BuyerDto`` carries both a numeric ``id`` and
    a string ``buyer_id`` (the platform's external reference) — the line item
    field is an integer, so the numeric id wins and a non-numeric fallback is
    dropped rather than sent as garbage.
    """
    rows = payload.get("buyers") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    options: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = row.get("id")
        if identifier is None and str(row.get("buyer_id") or "").isdigit():
            identifier = int(row["buyer_id"])
        if not isinstance(identifier, int):
            continue
        options.append({"id": identifier, "name": row.get("name") or row.get("buyer_name") or str(identifier)})
    return options


def _improvedigital_paginate(fetch_page, envelope_key: str, page_size: int = 100, max_rows: int = 10000) -> list:
    """Exhaust a 360Yield offset/limit-paginated list endpoint.

    The server clamps ``limit`` (observed max 100), so a page shorter than the
    requested size is NOT a termination signal. Advance by what was actually
    returned and stop on an empty page, on reaching the envelope's
    ``totalNumberOfElemements`` (sic — upstream typo), or on a page that adds
    no unseen ids (guards against a server that ignores ``offset``);
    ``max_rows`` backstops a runaway loop.
    """

    def _row_key(row: dict) -> tuple:
        # Dictionary rows (RegionDto/CountryDto) carry only ``name`` — keying
        # on id alone would collapse them all to None and stop pagination
        # after the first page.
        return (row.get("id"), row.get("name"))

    rows: list = []
    seen_ids: set = set()
    offset = 0
    while len(rows) < max_rows:
        payload = fetch_page(limit=page_size, offset=offset)
        page = _improvedigital_rows(payload, envelope_key)
        if not page:
            break
        fresh = [row for row in page if not isinstance(row, dict) or _row_key(row) not in seen_ids]
        seen_ids.update(_row_key(row) for row in fresh if isinstance(row, dict))
        if not fresh:
            break
        rows.extend(fresh)
        total = payload.get("totalNumberOfElemements") if isinstance(payload, dict) else None
        if isinstance(total, int) and len(rows) >= total:
            break
        offset += len(page)
    return rows[:max_rows]


@adapters_bp.route("/api/tenant/<tenant_id>/adapters/improvedigital/test-connection", methods=["POST"])
@require_tenant_access(api_mode=True)
def test_improvedigital_connection(tenant_id, **kwargs):
    """Verify Improve Digital OAuth2 credentials by minting a bearer and
    probing a Classic-campaigns read; on success, best-effort identify the
    API user via ``/lookup/v1/user-details``.

    Read-only probe — never writes to AdapterConfig — so it opts into the
    embedded-write gate.
    """
    try:
        data = request.get_json() or {}
        client_kwargs, cred_error = _resolve_improvedigital_credentials(tenant_id, data)
        if cred_error:
            return jsonify({"success": False, "error": cred_error}), 400
        assert client_kwargs is not None  # narrowed by the cred_error check

        from src.adapters.improvedigital import ImproveDigitalClient, ImproveDigitalError

        client = ImproveDigitalClient(**client_kwargs)
        try:
            status, _body = client.probe("GET", "/rtb/v1/classic/campaigns?limit=1")
        except ImproveDigitalError as exc:
            logger.warning(
                "Improve Digital credential probe failed: tenant_id=%s status=%s error=%s body_excerpt=%s",
                tenant_id,
                exc.status_code,
                exc,
                safe_upstream_body_excerpt(exc.body),
            )
            return jsonify({"success": False, "error": "Improve Digital rejected the credentials"}), 200

        if status >= 400:
            return jsonify({"success": False, "error": f"Improve Digital responded HTTP {status}"}), 200

        result: dict = {"success": True, "base_url": client._transport.base_url}
        try:
            details = client.lookups.user_details()
            result["user"] = {
                "user_id": details.get("user_id"),
                "name": f"{details.get('first_name', '')} {details.get('last_name', '')}".strip(),
                "business_unit": details.get("business_unit_name"),
                # Campaign booking requires a business_unit_id (layer-2 rule,
                # not in the create schema) — the API user's own unit is the
                # right default, so the UI auto-fills it from here. The
                # user_id doubles as the improve_demand_contact_id, and the
                # buyer list backs the Buyer ID picker.
                "business_unit_id": details.get("business_unit_id"),
                "buyers": _improvedigital_buyer_options(details),
            }
        except ImproveDigitalError:
            pass  # identity display is optional — credentials are already verified
        return jsonify(result)
    except Exception as e:
        logger.error(f"Improve Digital connection test failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": "Connection test failed (see server logs)"}), 500


@adapters_bp.route("/api/tenant/<tenant_id>/adapters/improvedigital/discover-buying-entities", methods=["POST"])
@require_tenant_access(api_mode=True)
def discover_improvedigital_buying_entities(tenant_id, **kwargs):
    """Discover buying entities — or, when ``buying_entity_id`` is submitted,
    that entity's offices (each carrying its ``improve_demand_contact_id``).

    Backs the cascading pickers in the adapter connection UI. Requires
    admin-scoped Improve Digital credentials; a 403 upstream is reported as
    ``discovery_available: false`` so the UI falls back to manual ID entry.

    Read-only — never writes to AdapterConfig — so it opts into the
    embedded-write gate.
    """
    try:
        data = request.get_json() or {}
        client_kwargs, cred_error = _resolve_improvedigital_credentials(tenant_id, data)
        if cred_error:
            return jsonify({"success": False, "error": cred_error}), 400
        assert client_kwargs is not None  # narrowed by the cred_error check

        from src.adapters.improvedigital import ImproveDigitalClient, ImproveDigitalError

        client = ImproveDigitalClient(**client_kwargs)
        try:
            if data.get("buying_entity_id"):
                rows = _improvedigital_paginate(
                    lambda **params: client.admin.list_buying_entity_offices(int(data["buying_entity_id"]), **params),
                    "buying_entity_offices",
                )
                offices = [
                    {
                        "id": row.get("id"),
                        "office": row.get("office"),
                        "buying_entity_id": row.get("buying_entity_id"),
                        "improve_demand_contact_id": row.get("improve_demand_contact_id"),
                        "billing_currency_code": row.get("billing_currency_code"),
                        "buying_types": row.get("buying_types") or [],
                        # Offices that pin a buyer let the picker fill Buyer ID
                        # from the office selection instead of a second lookup.
                        "buyers": _improvedigital_buyer_options(row),
                    }
                    for row in rows
                    if row.get("active") and "Classic" in (row.get("buying_types") or [])
                ]
                return jsonify({"success": True, "offices": offices})

            rows = _improvedigital_paginate(client.admin.list_buying_entities, "buying_entities_combo")
            entities = [{"id": row.get("id"), "name": row.get("name")} for row in rows]
            return jsonify({"success": True, "buying_entities": entities})
        except ImproveDigitalError as exc:
            if exc.status_code == 403:
                return jsonify(
                    {
                        "success": False,
                        "discovery_available": False,
                        "error": "Credentials lack Admin API scope — enter the IDs manually",
                    }
                )
            logger.warning(
                "Improve Digital buying-entity discovery failed: tenant_id=%s status=%s error=%s body_excerpt=%s",
                tenant_id,
                exc.status_code,
                exc,
                safe_upstream_body_excerpt(exc.body),
            )
            return jsonify({"success": False, "error": "Improve Digital rejected the discovery request"}), 200
    except Exception as e:
        logger.error(f"Improve Digital buying-entity discovery failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": "Discovery failed (see server logs)"}), 500


@adapters_bp.route("/api/tenant/<tenant_id>/adapters/improvedigital/discover-metadata", methods=["POST"])
@require_tenant_access(api_mode=True)
def discover_improvedigital_metadata(tenant_id, **kwargs):
    """Search the campaign-metadata dimensions (advertisers / agencies).

    Backs the Campaign Metadata pickers in the adapter connection UI. Both
    endpoints take a free-text ``search`` and return ``{id, name}`` rows —
    advertiser ids are UUID strings, agency ids are integers, so ids are
    passed through verbatim rather than coerced.

    Read-only — never writes to AdapterConfig — so it opts into the
    embedded-write gate.
    """
    try:
        data = request.get_json() or {}
        kind = str(data.get("kind") or "advertisers")
        if kind not in ("advertisers", "agencies"):
            return jsonify({"success": False, "error": f"Unknown metadata kind {kind!r}"}), 400

        client_kwargs, cred_error = _resolve_improvedigital_credentials(tenant_id, data)
        if cred_error:
            return jsonify({"success": False, "error": cred_error}), 400
        assert client_kwargs is not None  # narrowed by the cred_error check

        from src.adapters.improvedigital import ImproveDigitalClient, ImproveDigitalError

        client = ImproveDigitalClient(**client_kwargs)
        search = (data.get("search") or "").strip() or None
        try:
            fetch = client.metadata.list_advertisers if kind == "advertisers" else client.metadata.list_agencies
            rows = _improvedigital_rows(fetch(search), kind, "content", "data")
        except ImproveDigitalError as exc:
            if exc.status_code == 403:
                return jsonify(
                    {
                        "success": False,
                        "discovery_available": False,
                        "error": "Credentials lack metadata API scope — enter the values manually",
                    }
                )
            logger.warning(
                "Improve Digital metadata discovery failed: tenant_id=%s kind=%s status=%s error=%s body_excerpt=%s",
                tenant_id,
                kind,
                exc.status_code,
                exc,
                safe_upstream_body_excerpt(exc.body),
            )
            return jsonify({"success": False, "error": "Improve Digital rejected the metadata lookup"}), 200

        items = [
            {"id": row.get("id"), "name": row.get("name")}
            for row in rows
            if isinstance(row, dict) and row.get("id") is not None
        ]
        return jsonify({"success": True, "kind": kind, "items": items})
    except Exception as e:
        logger.error(f"Improve Digital metadata discovery failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": "Metadata discovery failed (see server logs)"}), 500


@adapters_bp.route("/api/tenant/<tenant_id>/adapters/improvedigital/inventory", methods=["GET"])
@require_tenant_access(api_mode=True)
def list_improvedigital_inventory(tenant_id, **kwargs):
    """Return locally-cached Improve Digital inventory entries for the
    product setup UI.

    Filterable by ``entity_type`` (publisher, placement, package, size).
    Optional ``parent_id`` narrows placements to one publisher. Optional
    ``q`` substring-matches the ``name`` field. Optional ``limit`` caps the
    returned rows AFTER filtering (the browse page passes it; the product
    pickers omit it and cache the full set client-side).
    """
    from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository

    entity_type = request.args.get("entity_type")
    parent_id = request.args.get("parent_id")
    q = request.args.get("q")
    limit = request.args.get("limit", type=int)

    if not entity_type:
        return jsonify({"success": False, "error": "entity_type query param is required"}), 400

    with get_db_session() as session:
        repo = ImproveDigitalInventoryRepository(session, tenant_id)
        rows = repo.list_by_type(entity_type, parent_id=parent_id)

    items = [
        {"entity_id": row.entity_id, "name": row.name, "parent_id": row.parent_id}
        for row in rows
        if not q or (row.name and q.lower() in row.name.lower())
    ]
    total = len(items)
    if limit is not None and limit >= 0:
        items = items[:limit]
    return jsonify({"success": True, "entity_type": entity_type, "count": total, "items": items})


@adapters_bp.route("/api/tenant/<tenant_id>/adapters/improvedigital/inventory-stats", methods=["GET"])
@require_tenant_access(api_mode=True)
def improvedigital_inventory_stats(tenant_id, **kwargs):
    """Quick stats for the Improve Digital inventory cache — row counts per
    entity type + last sync time. Feeds the Browse Inventory header and the
    Sync Inventory page without materializing the 20k+ cached rows."""
    from src.core.database.repositories.improvedigital_inventory import ImproveDigitalInventoryRepository

    with get_db_session() as session:
        repo = ImproveDigitalInventoryRepository(session, tenant_id)
        counts = repo.counts_by_type()
        last_synced = repo.latest_sync_at()

    return jsonify(
        {
            "success": True,
            "counts": counts,
            "total": sum(counts.values()),
            "last_synced_at": last_synced.isoformat() if last_synced else None,
        }
    )


@adapters_bp.route(
    "/api/tenant/<tenant_id>/adapters/improvedigital/packages/<int:package_id>/placements", methods=["GET"]
)
@require_tenant_access(api_mode=True)
def peek_improvedigital_package_placements(tenant_id, package_id, **kwargs):
    """Peek inside one placement package — live membership from the 360Yield
    API (``GET /rtb/v1/packages/{id}/placements``).

    On-demand per package the operator actually expands: package membership
    is dynamic on Improve Digital's side, so it is deliberately NOT part of
    the inventory sync (2k+ packages × one request each would eat the
    100-reads/60s quota for ~20 minutes per sweep).
    """
    try:
        client_kwargs, cred_error = _resolve_improvedigital_credentials(tenant_id, {})
        if cred_error:
            return jsonify({"success": False, "error": cred_error}), 400
        assert client_kwargs is not None  # narrowed by the cred_error check

        from src.adapters.improvedigital import ImproveDigitalClient, ImproveDigitalError

        client = ImproveDigitalClient(**client_kwargs)
        try:
            raw = client.inventory.package_placements(package_id)
        except ImproveDigitalError as exc:
            if exc.status_code == 404:
                return jsonify({"success": False, "error": "Package not found on Improve Digital"}), 404
            logger.warning(
                "Improve Digital package peek failed: tenant_id=%s package_id=%s status=%s error=%s",
                tenant_id,
                package_id,
                exc.status_code,
                exc,
            )
            return jsonify({"success": False, "error": "Improve Digital rejected the lookup"}), 200

        placements = [
            {
                "id": row.get("placement_id") if row.get("placement_id") is not None else row.get("id"),
                "name": row.get("placement_name") or row.get("name"),
                "site": row.get("site_name"),
                "publisher": row.get("publisher_name"),
            }
            for row in _improvedigital_rows(raw, "placements", "content")
        ]
        return jsonify({"success": True, "package_id": package_id, "count": len(placements), "placements": placements})
    except Exception as e:
        logger.error(f"Improve Digital package peek failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": "Package lookup failed (see server logs)"}), 500


@adapters_bp.route("/api/tenant/<tenant_id>/adapters/improvedigital/sync-inventory", methods=["POST"])
@require_tenant_access(api_mode=True)
def sync_improvedigital_inventory(tenant_id, **kwargs):
    """Enqueue a 360Yield buy-side inventory sweep and return immediately.

    The sweep runs in a background thread via the shared sync orchestration
    (adapter construction from stored config, SyncJob bookkeeping); rows
    are committed page-by-page so the cache fills progressively. Returns
    202 with the ``sync_id`` — the UI polls ``sync-status/<sync_id>`` for
    the outcome. Enqueueing is idempotent: if a sweep is already in flight
    its ``sync_id`` is returned instead of starting a duplicate.

    The cache feeds the Improve Digital product setup UI; it's not exposed
    to AdCP buyers (property discovery goes through AAO lookup).
    """
    from src.services.adapter_sync_orchestration import enqueue_adapter_sync

    try:
        sync_id = enqueue_adapter_sync(
            tenant_id=tenant_id,
            adapter_type="improvedigital",
            sync_kind="inventory",
            triggered_by="admin_button",
        )
        if sync_id is None:
            return (
                jsonify({"success": False, "error": "Improve Digital adapter is not configured for this tenant"}),
                400,
            )
        return jsonify({"success": True, "sync_id": sync_id, "status": "queued"}), 202
    except Exception as e:
        logger.error(f"Improve Digital inventory sync enqueue failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": "Sync failed to start (see server logs)"}), 500


@adapters_bp.route("/api/tenant/<tenant_id>/adapters/improvedigital/sync-status/<sync_id>", methods=["GET"])
@require_tenant_access(api_mode=True)
def improvedigital_sync_status(tenant_id, sync_id, **kwargs):
    """Poll one sync job's state — feeds the async Sync Inventory button.

    Counts/errors come from ``SyncJob.progress`` (stamped by the
    orchestrator when the run finishes); while the job is still running
    the UI shows live cache growth via ``inventory-stats`` instead.
    """
    from src.core.database.repositories.sync_job import SyncJobRepository

    with get_db_session() as session:
        job = SyncJobRepository(session, tenant_id).find_by_sync_id(sync_id)
        if job is None:
            return jsonify({"success": False, "error": "Unknown sync job"}), 404
        progress = job.progress or {}
        payload = {
            "success": True,
            "sync_id": job.sync_id,
            "status": job.status,
            "counts": progress.get("counts", {}),
            "errors": progress.get("errors", {}),
            "error_message": job.error_message,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        }
    return jsonify(payload)


def _improvedigital_reporting_payload(stat_rows, buys_by_campaign: dict, currency: str) -> dict:
    """Shape line-item stats cache rows into the reporting-page JSON.

    ``buys_by_campaign`` maps Classic campaign IDs to MediaBuy rows so each
    stats row can carry the buy it belongs to; rows whose campaign has no
    matching buy (e.g. booked outside salesagent) still render, unattributed.
    Spend is stored as micros — converted to currency units here, once.
    """
    rows = []
    total_impressions = 0
    total_clicks = 0
    total_spend = 0.0
    total_completed = 0
    for stat in stat_rows:
        impressions = int(stat.impressions or 0)
        clicks = int(stat.clicks) if stat.clicks is not None else None
        spend = round((stat.spend_micros or 0) / 1_000_000, 2)
        buy = buys_by_campaign.get(str(stat.campaign_id)) if stat.campaign_id else None
        rows.append(
            {
                "campaign_id": stat.campaign_id,
                "line_item_id": stat.line_item_id,
                "media_buy_id": buy.media_buy_id if buy else None,
                "order_name": buy.order_name if buy else None,
                "advertiser_name": buy.advertiser_name if buy else None,
                "impressions": impressions,
                "clicks": clicks,
                "ctr": round(clicks / impressions * 100, 2) if clicks and impressions else None,
                "completed_views": int(stat.completed_views) if stat.completed_views is not None else None,
                "spend": spend,
                "currency": stat.currency or currency,
                "as_of": stat.as_of.isoformat() if stat.as_of else None,
            }
        )
        total_impressions += impressions
        total_clicks += clicks or 0
        total_spend += spend
        total_completed += int(stat.completed_views or 0)
    return {
        "rows": rows,
        "totals": {
            "impressions": total_impressions,
            "clicks": total_clicks,
            "ctr": round(total_clicks / total_impressions * 100, 2) if total_impressions else None,
            "completed_views": total_completed,
            "spend": round(total_spend, 2),
        },
        "currency": currency,
    }


@adapters_bp.route("/api/tenant/<tenant_id>/adapters/improvedigital/reporting", methods=["GET"])
@require_tenant_access(api_mode=True)
def get_improvedigital_reporting(tenant_id, **kwargs):
    """Serve the Report-API stats cache for the reporting page.

    Reads ``improvedigital_line_item_stats`` (populated by the reporting
    sync — no upstream call here, so the page loads instantly) and joins
    campaigns to media buys via the ``improvedigital_<campaign_id>``
    reference on the packages' ``platform_order_id`` / ``media_buy_id``.
    """
    from src.adapters.improvedigital._buy_refs import campaign_id_from_refs, platform_order_ref
    from src.core.database.repositories.adapter_config import AdapterConfigRepository
    from src.core.database.repositories.improvedigital_line_item_stats import (
        ImproveDigitalLineItemStatsRepository,
    )
    from src.core.database.repositories.media_buy import MediaBuyRepository

    with get_db_session() as session:
        repo = ImproveDigitalLineItemStatsRepository(session, tenant_id)
        stat_rows = repo.list_all()
        last_synced_at = repo.latest_sync_at()

        buy_repo = MediaBuyRepository(session, tenant_id)
        buys = buy_repo.list_all()
        packages_by_buy = buy_repo.get_packages_for_ids([b.media_buy_id for b in buys]) if buys else {}
        buys_by_campaign: dict = {}
        for buy in buys:
            campaign_id = campaign_id_from_refs(
                platform_order_ref(packages_by_buy.get(buy.media_buy_id)), buy.media_buy_id
            )
            if campaign_id is not None:
                buys_by_campaign[campaign_id] = buy

        config_row = AdapterConfigRepository(session, tenant_id).find_by_tenant()
        currency = str((config_row.config_json or {}).get("currency") or "EUR") if config_row else "EUR"

        payload = _improvedigital_reporting_payload(stat_rows, buys_by_campaign, currency)

    payload["success"] = True
    payload["last_synced_at"] = last_synced_at.isoformat() if last_synced_at else None
    return jsonify(payload)


@adapters_bp.route("/api/tenant/<tenant_id>/adapters/improvedigital/sync-reporting", methods=["POST"])
@require_tenant_access(api_mode=True)
def sync_improvedigital_reporting(tenant_id, **kwargs):
    """Pull fresh delivery metrics from the 360Yield Report API and upsert
    the ``improvedigital_line_item_stats`` cache feeding the reporting page
    and ``get_media_buy_delivery``.

    Returns 503 when the Report API scope is still pending for this OAuth2
    client.
    """
    from src.services.adapter_sync_orchestration import SyncAlreadyRunning, execute_adapter_sync

    try:
        result = execute_adapter_sync(
            tenant_id=tenant_id,
            adapter_type="improvedigital",
            sync_kind="reporting",
            triggered_by="admin_button",
        )
        if result is None:
            return (
                jsonify({"success": False, "error": "Improve Digital adapter is not configured for this tenant"}),
                400,
            )
        if result.scope_pending:
            return (
                jsonify(
                    {
                        "success": False,
                        "scope_pending": True,
                        "sync_id": result.sync_id,
                        "error": result.errors.get("scope", "Report API scope grant pending"),
                    }
                ),
                503,
            )
        return jsonify(
            {
                "success": result.succeeded,
                "sync_id": result.sync_id,
                "line_items_updated": result.counts.get("line_items", 0),
                "campaigns_covered": result.counts.get("campaigns", 0),
                "error": next(iter(result.errors.values()), None) if result.errors else None,
            }
        )
    except SyncAlreadyRunning as exc:
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"A reporting sync is already running ({exc.sync_id}) — wait for it to finish",
                }
            ),
            409,
        )
    except ValidationError as exc:
        return jsonify({"success": False, "error": f"Stored config is invalid: {exc}"}), 400
    except Exception as e:
        logger.error(f"Improve Digital reporting sync failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": "Sync failed (see server logs)"}), 500
