"""
AdCP JSON Schema Validator for E2E Tests

Validates AdCP protocol requests and responses against the official AdCP
JSON schemas **bundled with the installed ``adcp`` SDK** (``adcp/_schemas/
<major.minor>/``). The bundle is the exact spec release the server speaks
(``adcp.get_adcp_spec_version()``), so the validator can never drift from the
implementation the way a network-cached copy of ``/schemas/v1`` (which now
aliases a 3.2 prerelease) did.

Key Features:
- Zero network access: schemas come from the SDK wheel, so ``offline_mode``
  is always effectively on and CI cannot be broken by the registry moving.
- Full ``$ref`` resolution across the bundle (``../core/*.json``, enums,
  pricing options, ...) through a ``referencing`` registry.
- Performance-optimized with compiled validators.
- Detailed error reporting with JSON path locations.

Usage:
    validator = AdCPSchemaValidator()
    await validator.validate_response("get-products", response_data)
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

import pytest
import referencing
from adcp import get_adcp_spec_version
from adcp.validation.version import resolve_bundle_key
from jsonschema.validators import Draft7Validator
from referencing.exceptions import NoSuchResource
from referencing.jsonschema import DRAFT7

# Canonical host of the published spec. Only used to give every bundled
# schema a stable ``$id`` so relative ``$ref`` values resolve, and to accept
# absolute registry URLs in ``get_schema()``.
SCHEMA_HOST = "https://adcontextprotocol.org"

# ``/schemas/<version>/<rel>`` — the ``<version>`` segment may be ``v1``,
# ``latest``, ``3.1``, ``3.1.15`` ...; every spelling maps onto the bundle.
_REGISTRY_PATH_RE = re.compile(r"^/?schemas/[^/]+/(?P<rel>.+)$")

# Aliases callers historically passed as ``adcp_version``; all of them mean
# "the spec release the installed SDK implements".
_SDK_VERSION_ALIASES = frozenset({"", "v1", "latest", "current", "sdk"})


class SchemaError(Exception):
    """Base exception for schema validation errors."""

    pass


class SchemaDownloadError(SchemaError):
    """Raised when a schema cannot be located in the SDK bundle.

    The name is historical (schemas used to be downloaded); it is kept so
    callers that catch it keep working.
    """

    pass


class SchemaValidationError(SchemaError):
    """Raised when JSON validation fails."""

    def __init__(self, message: str, validation_errors: list[str], json_path: str = ""):
        super().__init__(message)
        self.validation_errors = validation_errors
        self.json_path = json_path


def _sdk_bundle_key() -> str:
    """Bundle key (``MAJOR.MINOR``) of the spec release the installed SDK speaks."""
    return resolve_bundle_key(get_adcp_spec_version())


def _locate_bundle(bundle_key: str) -> Path:
    """Return the on-disk root of the SDK's schema bundle for ``bundle_key``."""
    try:
        packaged = files("adcp") / "_schemas" / bundle_key
        with as_file(packaged) as path:
            root = Path(path)
    except (ModuleNotFoundError, FileNotFoundError, OSError) as exc:
        raise SchemaDownloadError(f"adcp SDK does not bundle schemas for AdCP {bundle_key}: {exc}") from exc
    if not (root / "index.json").is_file():
        raise SchemaDownloadError(f"adcp SDK bundle for AdCP {bundle_key} has no index.json at {root}")
    return root


class AdCPSchemaValidator:
    """
    Validator for AdCP protocol JSON schemas.

    Loads the schema bundle shipped inside the installed ``adcp`` package and
    validates against it. The public surface (``get_schema_index``,
    ``get_schema``, ``validate_request``, ``validate_response``) is unchanged
    from the network-backed predecessor so existing tests keep working.
    """

    def __init__(self, cache_dir: Path | None = None, offline_mode: bool = False, adcp_version: str = "v1"):
        """
        Initialize the schema validator.

        Args:
            cache_dir: Ignored (kept for signature compatibility). Schemas are
                read from the SDK wheel, never from a writable cache.
            offline_mode: Ignored (kept for signature compatibility). The
                validator never touches the network.
            adcp_version: Spec release to validate against. ``"v1"`` /
                ``"latest"`` (the historical defaults) mean the release the
                installed SDK implements; a concrete release (``"3.1"``,
                ``"3.0.2"``) selects that bundle if the SDK ships it.
        """
        # Always offline by construction: nothing is ever fetched over the network.
        self.offline_mode = True
        requested = (adcp_version or "").strip()
        self.bundle_key = _sdk_bundle_key() if requested in _SDK_VERSION_ALIASES else resolve_bundle_key(requested)
        self.adcp_version = self.bundle_key
        self.schema_root = _locate_bundle(self.bundle_key)
        # ``cache_dir`` is retained as an attribute for callers that print it.
        self.cache_dir = Path(cache_dir) if cache_dir is not None else self.schema_root

        # Schema registry (keyed by bundle-relative path) and compiled validators cache
        self._schema_registry: dict[str, dict] = {}
        self._compiled_validators: dict[str, Draft7Validator] = {}
        self._index_cache: dict | None = None
        self._basename_index: dict[str, list[str]] | None = None

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        return None

    # ------------------------------------------------------------------
    # Reference normalisation
    # ------------------------------------------------------------------

    def _relative_ref(self, schema_ref: str) -> str:
        """Normalise any accepted ``$ref`` spelling to a bundle-relative path.

        Accepts ``media-buy/x.json`` (index refs), ``../core/x.json``
        (intra-bundle refs), ``/schemas/v1/media-buy/x.json`` (registry
        paths) and ``https://adcontextprotocol.org/schemas/3.1/...``
        (absolute registry URLs). Fragments are dropped.
        """
        ref = schema_ref.split("#", 1)[0].strip()
        if ref.startswith(SCHEMA_HOST):
            ref = ref[len(SCHEMA_HOST) :]
        match = _REGISTRY_PATH_RE.match(ref)
        if match:
            ref = match.group("rel")
        ref = ref.lstrip("/")
        while ref.startswith("./"):
            ref = ref[2:]
        return ref

    def _schema_id(self, rel: str) -> str:
        """Stable ``$id`` for a bundle-relative path (base for relative refs)."""
        return f"{SCHEMA_HOST}/schemas/{self.bundle_key}/{rel}"

    def _basename_lookup(self, rel: str) -> str | None:
        """Find ``rel``'s basename elsewhere in the bundle.

        The registry historically served every task under ``media-buy/``;
        the bundle files creative and signals tasks under their own groups
        (``creative/sync-creatives-request.json``). Resolve by basename when
        the requested group is wrong, ignoring the self-contained
        ``bundled/`` copies so one canonical file backs each ref.
        """
        if self._basename_index is None:
            index: dict[str, list[str]] = {}
            for path in sorted(self.schema_root.rglob("*.json")):
                relative = path.relative_to(self.schema_root).as_posix()
                if relative.startswith("bundled/"):
                    continue
                index.setdefault(path.name, []).append(relative)
            self._basename_index = index
        candidates = self._basename_index.get(Path(rel).name, [])
        return candidates[0] if len(candidates) == 1 else None

    def _load_schema_file(self, schema_ref: str) -> tuple[str, dict[str, Any]]:
        """Load a schema from the bundle, returning ``(relative_path, schema)``."""
        rel = self._relative_ref(schema_ref)
        path = self.schema_root / rel
        if not path.is_file():
            alternative = self._basename_lookup(rel)
            if alternative is None:
                raise SchemaDownloadError(
                    f"Schema {schema_ref!r} not found in adcp SDK bundle for AdCP {self.bundle_key} ({self.schema_root})"
                )
            rel, path = alternative, self.schema_root / alternative
        if rel in self._schema_registry:
            return rel, self._schema_registry[rel]
        try:
            with open(path) as f:
                schema = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise SchemaDownloadError(f"Bundled schema {rel} is unreadable: {exc}") from exc
        # Bundled files carry no ``$id``; inject the registry-shaped one so
        # ``../core/*.json`` refs resolve against the right base.
        schema.setdefault("$id", self._schema_id(rel))
        self._schema_registry[rel] = schema
        return rel, schema

    # ------------------------------------------------------------------
    # Public loading API
    # ------------------------------------------------------------------

    async def get_schema_index(self) -> dict[str, Any]:
        """Get the schema index shipped with the SDK bundle."""
        if self._index_cache is None:
            _rel, self._index_cache = self._load_schema_file("index.json")
        return self._index_cache

    async def get_schema(self, schema_ref: str) -> dict[str, Any]:
        """Get a schema by reference (same object on repeated calls)."""
        _rel, schema = self._load_schema_file(schema_ref)
        return schema

    def _get_compiled_validator(self, schema: dict[str, Any]) -> Draft7Validator:
        """Get a compiled validator for a schema, with caching."""
        # Create a hash of the schema for caching
        schema_hash = hashlib.md5(json.dumps(schema, sort_keys=True).encode()).hexdigest()

        if schema_hash not in self._compiled_validators:

            def _retrieve(uri: str) -> referencing.Resource:
                """Retrieve a referenced schema from the SDK bundle."""
                try:
                    _rel, resolved = self._load_schema_file(uri)
                except SchemaDownloadError as exc:
                    raise NoSuchResource(ref=uri) from exc
                return DRAFT7.create_resource(resolved)

            registry = referencing.Registry(retrieve=_retrieve)
            # Seed the registry with the root schema
            root_resource = DRAFT7.create_resource(schema)
            root_id = schema.get("$id", "")
            if root_id:
                registry = registry.with_resource(root_id, root_resource)

            self._compiled_validators[schema_hash] = Draft7Validator(schema, registry=registry)

        return self._compiled_validators[schema_hash]

    async def _find_schema_ref_for_task(self, task_name: str, request_or_response: str) -> str | None:
        """Find the schema reference for a specific task and type."""
        index = await self.get_schema_index()
        groups = index.get("schemas", {})

        # Task groups in registry order: media-buy first (historical home of
        # every task), then the groups the spec later split out.
        for group in ("media-buy", "creative", "signals", "account"):
            tasks = groups.get(group, {}).get("tasks", {})
            task_info = tasks.get(task_name)
            if task_info and request_or_response in task_info:
                return task_info[request_or_response]["$ref"]

        return None

    # ------------------------------------------------------------------
    # Validation API
    # ------------------------------------------------------------------

    async def validate_request(self, task_name: str, request_data: dict[str, Any]) -> None:
        """
        Validate a request against AdCP schema.

        Args:
            task_name: Name of the AdCP task (e.g., "get-products")
            request_data: The request data to validate

        Raises:
            SchemaValidationError: If validation fails
        """
        schema_ref = await self._find_schema_ref_for_task(task_name, "request")
        if not schema_ref:
            # Don't fail if schema not found - log warning instead
            print(f"Warning: No request schema found for task '{task_name}'")
            return

        await self._preload_schema_references(schema_ref)
        await self._validate_against_schema(schema_ref, request_data, f"{task_name} request")

    async def validate_response(self, task_name: str, response_data: dict[str, Any]) -> None:
        """
        Validate a response against AdCP schema.

        This method understands protocol layering - it will extract the AdCP payload
        from MCP/A2A wrapper fields and validate only the payload against the schema.

        Args:
            task_name: Name of the AdCP task (e.g., "get-products")
            response_data: The response data to validate (may include protocol wrapper fields)

        Raises:
            SchemaValidationError: If validation fails
        """
        schema_ref = await self._find_schema_ref_for_task(task_name, "response")
        if not schema_ref:
            # Don't fail if schema not found - log warning instead
            print(f"Warning: No response schema found for task '{task_name}'")
            return

        # Extract AdCP payload from protocol wrapper if present
        adcp_payload = self._extract_adcp_payload(response_data)

        await self._preload_schema_references(schema_ref)
        await self._validate_against_schema(schema_ref, adcp_payload, f"{task_name} response")

    def _extract_adcp_payload(self, response_data: dict[str, Any]) -> dict[str, Any]:
        """
        Extract the AdCP payload from protocol wrapper fields.

        MCP and A2A protocols may add wrapper fields like:
        - message: Human-readable message from the transport layer
        - context_id: Session continuity identifier
        - errors: Transport-layer errors (not part of AdCP spec)
        - clarification_needed: Non-spec field that should be removed

        This method removes these protocol-layer fields and returns only
        the AdCP payload for validation.

        Args:
            response_data: The full response including protocol wrapper fields

        Returns:
            The AdCP payload with protocol-layer fields removed
        """
        # List of known protocol-layer fields that are not part of AdCP spec
        protocol_fields = {
            "message",  # MCP/A2A transport layer message
            "context_id",  # MCP session continuity
            "clarification_needed",  # Non-spec field
            "errors",  # Transport-layer errors (not in AdCP spec)
            # Note: Some AdCP responses do have "error" fields defined in spec,
            # but "errors" (plural) is typically a transport-layer addition
        }

        # Create a copy of the response without protocol fields
        adcp_payload = {}
        for key, value in response_data.items():
            if key not in protocol_fields:
                adcp_payload[key] = value

        return adcp_payload

    async def _preload_schema_references(self, schema_ref: str, _visited: set[str] | None = None) -> None:
        """
        Recursively load every schema referenced by ``schema_ref``.

        Loading is local and cheap; doing it up front surfaces a broken
        bundle reference as a clear ``SchemaDownloadError`` instead of an
        opaque resolution failure mid-validation.
        """
        if _visited is None:
            _visited = set()

        rel, schema = self._load_schema_file(schema_ref)
        if rel in _visited:
            return
        _visited.add(rel)

        base_dir = Path(rel).parent
        for ref in self._find_schema_references(schema):
            if ref.startswith("#"):
                continue  # intra-document pointer
            target = ref if ("://" in ref or ref.startswith("/")) else (base_dir / ref.split("#", 1)[0]).as_posix()
            # Collapse ``a/../b`` produced by relative refs.
            target = Path(target).as_posix() if "://" in target else _normalize_posix(target)
            await self._preload_schema_references(target, _visited)

    def _find_schema_references(self, schema: dict[str, Any]) -> list[str]:
        """Find all $ref references in a schema recursively."""
        refs = []

        def find_refs_recursive(obj):
            if isinstance(obj, dict):
                if "$ref" in obj and isinstance(obj["$ref"], str):
                    refs.append(obj["$ref"])
                for value in obj.values():
                    find_refs_recursive(value)
            elif isinstance(obj, list):
                for item in obj:
                    find_refs_recursive(item)

        find_refs_recursive(schema)
        return refs

    async def _validate_against_schema(self, schema_ref: str, data: dict[str, Any], context: str = "") -> None:
        """
        Validate data against a specific schema reference.

        Args:
            schema_ref: Reference to the schema to validate against
            data: Data to validate
            context: Context string for error messages

        Raises:
            SchemaValidationError: If validation fails
        """
        try:
            schema = await self.get_schema(schema_ref)
            validator = self._get_compiled_validator(schema)

            errors = list(validator.iter_errors(data))
            if errors:
                error_messages = []
                for error in errors:
                    # Build JSON path
                    path = ".".join(str(p) for p in error.absolute_path)
                    if not path:
                        path = "root"

                    # Include more detailed error information
                    error_msg = f"At {path}: {error.message}"
                    if hasattr(error, "schema_path") and error.schema_path:
                        schema_path = ".".join(str(p) for p in error.schema_path)
                        error_msg += f" (schema path: {schema_path})"

                    error_messages.append(error_msg)

                raise SchemaValidationError(
                    f"Schema validation failed for {context}", error_messages, json_path=path if errors else ""
                )

        except SchemaDownloadError:
            # Re-raise schema lookup errors
            raise
        except SchemaValidationError:
            # Re-raise schema validation errors without wrapping them
            raise
        except Exception as e:
            raise SchemaValidationError(f"Unexpected error validating {context}: {e}", [str(e)]) from e


def _normalize_posix(path: str) -> str:
    """Collapse ``.`` / ``..`` segments of a bundle-relative POSIX path."""
    parts: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    return "/".join(parts)


# Decorator functions for easy integration with tests


def validate_adcp_request(task_name: str):
    """
    Decorator to validate AdCP request data.

    Usage:
        @validate_adcp_request("get-products")
        async def test_method(self):
            # Test implementation
            pass
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract request data from kwargs or test method
            # This would need to be integrated with the specific test patterns
            result = await func(*args, **kwargs)
            return result

        return wrapper

    return decorator


def validate_adcp_response(task_name: str):
    """
    Decorator to validate AdCP response data.

    Usage:
        @validate_adcp_response("get-products")
        async def test_method(self):
            # Test returns response data
            return response_data
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            result = await func(*args, **kwargs)

            # Validate the result if it looks like response data
            if isinstance(result, dict):
                async with AdCPSchemaValidator() as validator:
                    await validator.validate_response(task_name, result)

            return result

        return wrapper

    return decorator


# Test fixtures for pytest integration


@pytest.fixture
async def adcp_validator():
    """Pytest fixture providing an AdCP schema validator."""
    async with AdCPSchemaValidator() as validator:
        yield validator


@pytest.fixture
async def adcp_validator_offline():
    """Pytest fixture providing an offline AdCP schema validator."""
    async with AdCPSchemaValidator(offline_mode=True) as validator:
        yield validator


# Utility functions for test setup


async def preload_schemas(task_names: list[str] | None = None, load_all: bool = True, adcp_version: str = "v1"):
    """
    Load (and report on) the bundled task schemas.

    Kept for CLI/debugging parity with the network-backed predecessor: it
    walks the index and loads every task request/response schema so a
    broken bundle reference is reported eagerly.

    Args:
        task_names: List of task names to load. If None, loads common tasks.
        load_all: If True, loads all task schemas from the index.
        adcp_version: AdCP schema version to use (``"v1"`` = SDK release).
    """
    async with AdCPSchemaValidator(adcp_version=adcp_version) as validator:
        index = await validator.get_schema_index()
        groups = index.get("schemas", {})

        if load_all:
            print(f"📥 Loading ALL AdCP {validator.bundle_key} task schemas from the SDK bundle...")
            for group_name in ("media-buy", "creative", "signals"):
                print(f"\n📁 {group_name.upper()} TASK SCHEMAS:")
                for task_name, task_info in groups.get(group_name, {}).get("tasks", {}).items():
                    for req_resp in ["request", "response"]:
                        if req_resp in task_info:
                            try:
                                await validator.get_schema(task_info[req_resp]["$ref"])
                                print(f"  ✓ {task_name}-{req_resp}")
                            except Exception as e:
                                print(f"  ⚠ {task_name}-{req_resp}: {e}")
        else:
            if task_names is None:
                task_names = [
                    "get-products",
                    "list-creative-formats",
                    "create-media-buy",
                    "sync-creatives",
                    "update-media-buy",
                    "get-media-buy-delivery",
                ]

            print(f"📥 Loading schemas for specific tasks: {task_names}")
            for task_name in task_names:
                try:
                    for req_resp in ["request", "response"]:
                        schema_ref = await validator._find_schema_ref_for_task(task_name, req_resp)
                        if schema_ref:
                            await validator.get_schema(schema_ref)
                            print(f"✓ Loaded {task_name} {req_resp} schema")
                except Exception as e:
                    print(f"⚠ Failed to load {task_name}: {e}")

        print("\n🎉 Schema loading completed!")
        print(f"📦 Total schemas loaded: {len(validator._schema_registry)}")
        print(f"💾 Bundle location: {validator.schema_root}")


if __name__ == "__main__":
    """CLI for checking the bundled schemas."""
    import asyncio

    async def main():
        print("AdCP Schema Validator - loading bundled schemas...")
        await preload_schemas()
        print("Schema loading completed!")

    asyncio.run(main())
