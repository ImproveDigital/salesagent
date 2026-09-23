"""Bridge between salesagent's named creative formats and AdCP 3.1 canonical format options.

adcp 7 (AdCP 3.1.15) makes *canonical* creative identity the framework's
native contract:

* products declare ``format_options[]`` (a ``format_kind`` such as ``image``
  or ``video_hosted`` plus ``params``) instead of ``format_ids[]``;
* creatives carry ``format_kind`` + ``format_option_ref`` instead of ``format_id``;
* packages carry ``format_option_refs[]`` instead of ``format_ids[]``.

The SDK dispatcher negotiates a *creative dialect* per request. Canonical
buyers (Interchange, any 3.1 client) send and expect canonical identity.
Legacy buyers (3.0, or 3.1 with legacy evidence) still send ``format_id`` /
``format_ids``; the dispatcher rewrites those to canonical *before* the
platform runs (``normalize_legacy_creative_request``) and projects the
platform's canonical result back to legacy *after* (``project_canonical_
response_to_legacy``). Both directions need adopter-supplied mappings for
formats the SDK's bundled AAO catalog does not know, exposed as two
attributes the handler reads off the platform object:

* ``legacy_format_converter`` — named format → canonical declaration;
* ``canonical_format_legacy_resolver`` — canonical option → named format.

salesagent's business layer (``src/core/tools/*``) still speaks the named
format shape end to end (``FormatId`` on products, creatives, packages).
Rather than rewrite every impl, this module keeps the translation at the
``core/platforms`` boundary where the framework hands us requests and takes
our responses:

* :func:`legacy_request_payload` turns the canonical request the SDK gives
  the platform back into the legacy shape the impls expect;
* :func:`canonicalize_wire_response` turns the impls' legacy wire dict into
  the canonical shape the SDK expects a 3.1 platform to return.

Canonical ``format_option_id`` values are the SDK's deterministic
``migrated_<hash>`` of the full legacy tuple, so the reverse mapping can
always be rebuilt from the format catalog after a restart; an in-memory
index caches every tuple we have projected in this process.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from adcp.canonical_formats import (
    CanonicalFormatLegacyResolutionContext,
    LegacyFormatConversionContext,
    migrated_format_option_id,
    project_legacy_format_id,
)
from adcp.types import Format as CanonicalFormat
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import Features
from adcp.types.generated_poc.core.format_id import FormatReferenceStructuredObject
from adcp.types.legacy import LegacyFormatId
from pydantic import BaseModel

from src.core.exceptions import AdCPInvalidRequestError

logger = logging.getLogger(__name__)

# Wire collections that carry product-level declarations. Placements narrow a
# product's declarations, so they are converted with the same helper.
_PRODUCT_COLLECTIONS = ("products",)
_PACKAGE_COLLECTIONS = ("packages", "affected_packages")

# Canonical kinds inferred from a format's asset types when the format carries
# no SDK ``canonical`` annotation. Order matters: tag-based kinds win over
# hosted kinds, and any grouped image slot marks a carousel.
_ASSET_KIND_RULES: tuple[tuple[str, str], ...] = (
    ("vast", "video_vast"),
    ("daast", "audio_daast"),
    ("video", "video_hosted"),
    ("audio", "audio_hosted"),
    ("html", "html5"),
    ("javascript", "display_tag"),
    ("image", "image"),
)


# ---------------------------------------------------------------------------
# Capability declaration
# ---------------------------------------------------------------------------


class CanonicalCreativeFeatures(Features):
    """``media_buy.features`` with the AdCP 3.1 ``canonical_creatives`` flag.

    The generated ``Features`` model drops unknown keys, so the flag cannot be
    passed as a plain field. The SDK's dialect resolver reads it through the
    platform's ``capabilities.media_buy`` (dumped with ``serialize_as_any``), and
    without it a 3.1 request with no format evidence cannot be assigned a
    creative dialect at all ("AdCP 3.1 does not establish a creative dialect").
    Declaring it makes 3.1 buyers canonical by contract; legacy buyers negotiate
    3.0 (explicitly, or via :func:`prepare_legacy_request`).
    """

    canonical_creatives: bool = True


# ---------------------------------------------------------------------------
# Legacy tuple helpers
# ---------------------------------------------------------------------------


def legacy_ref(value: Any) -> LegacyFormatId:
    """Coerce a dict / ``FormatId`` / generated tuple into a ``LegacyFormatId``."""
    if isinstance(value, LegacyFormatId):
        return value
    if isinstance(value, FormatReferenceStructuredObject):
        return LegacyFormatId.model_validate(value.model_dump(mode="json", exclude_none=True))
    if hasattr(value, "model_dump"):
        return LegacyFormatId.model_validate(value.model_dump(mode="json", exclude_none=True))
    return LegacyFormatId.model_validate(value)


def _ref_wire(ref: LegacyFormatId) -> dict[str, Any]:
    return ref.model_dump(mode="json", exclude_none=True)


def _current_tenant_id() -> str | None:
    from src.core.config_loader import current_tenant

    tenant = current_tenant.get()
    return tenant.get("tenant_id") if isinstance(tenant, dict) else None


def _publisher_domain(tenant_id: str | None) -> str:
    """Domain namespace for ``scope: publisher`` option references (the seller)."""
    from src.core.config_loader import current_tenant
    from src.core.domain_config import get_sales_agent_domain

    tenant = current_tenant.get()
    if isinstance(tenant, dict) and (tenant_id is None or tenant.get("tenant_id") == tenant_id):
        virtual_host = tenant.get("virtual_host")
        if isinstance(virtual_host, str) and virtual_host:
            return virtual_host.lower()
        subdomain = tenant.get("subdomain")
        base = get_sales_agent_domain()
        if isinstance(subdomain, str) and subdomain and base:
            return f"{subdomain}.{base}".lower()
    return (get_sales_agent_domain() or "sales-agent.invalid").lower()


# ---------------------------------------------------------------------------
# Named format → canonical declaration (the SDK's ``legacy_format_converter``)
# ---------------------------------------------------------------------------


def _salesagent_format_for(ref: LegacyFormatId, tenant_id: str | None) -> Any | None:
    """Look up our ``Format`` object for a legacy tuple, or ``None`` if unknown."""
    from src.core.canonical_formats import is_reference_creative_agent_url
    from src.core.standard_formats import get_standard_format

    if is_reference_creative_agent_url(ref.agent_url):
        fmt = get_standard_format(ref.id)
        if fmt is not None:
            return fmt
    try:
        from src.core.format_resolver import get_format

        return get_format(ref.id, agent_url=str(ref.agent_url), tenant_id=tenant_id)
    except Exception as exc:  # unknown format / registry unavailable — caller decides
        logger.debug("canonical-formats: no salesagent format for %s/%s: %s", ref.agent_url, ref.id, exc)
        return None


def _attr(obj: Any, name: str) -> Any:
    """Read ``name`` from a pydantic model or a plain catalog dict."""
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _asset_entries(fmt: Any) -> list[Any]:
    assets = _attr(fmt, "assets") or []
    return list(assets) if isinstance(assets, (list, tuple)) else []


# Last-resort kind inference from the named format's id when the catalog entry
# carries neither a ``canonical`` annotation nor typed assets (e.g. a creative
# agent that only returns ``{id, name}``). Mirrors the SDK's own
# ``display_<w>x<h>`` → image rule for the AAO namespace.
_ID_KIND_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(display|banner|image)(_|$)"), "image"),
    (re.compile(r"^(html|html5|rich_media)(_|$)"), "html5"),
    (re.compile(r"^video(_|$).*vast"), "video_vast"),
    (re.compile(r"^video(_|$)"), "video_hosted"),
    (re.compile(r"^audio(_|$).*(vast|daast)"), "audio_daast"),
    (re.compile(r"^audio(_|$)"), "audio_hosted"),
    (re.compile(r"^native(_|$)"), "native_in_feed"),
)
_ID_SIZE = re.compile(r"(?:^|_)([1-9][0-9]*)x([1-9][0-9]*)(?:_|$)")


def _kind_from_id(format_id: str) -> str | None:
    for pattern, kind in _ID_KIND_RULES:
        if pattern.search(format_id):
            return kind
    return None


def _asset_type(asset: Any) -> str | None:
    value = asset.get("asset_type") if isinstance(asset, Mapping) else getattr(asset, "asset_type", None)
    return getattr(value, "value", value)


def _nested_assets(asset: Any) -> list[Any]:
    nested = asset.get("assets") if isinstance(asset, Mapping) else getattr(asset, "assets", None)
    return list(nested or [])


def _infer_kind_from_assets(fmt: Any) -> str | None:
    entries = _asset_entries(fmt)
    grouped_images = False
    types: set[str] = set()
    for asset in entries:
        kind = _asset_type(asset)
        if kind:
            types.add(kind)
        item_type = asset.get("item_type") if isinstance(asset, Mapping) else getattr(asset, "item_type", None)
        if getattr(item_type, "value", item_type) == "repeatable_group":
            nested_types = {t for t in (_asset_type(a) for a in _nested_assets(asset)) if t}
            types |= nested_types
            if "image" in nested_types:
                grouped_images = True
    for asset_type, kind in _ASSET_KIND_RULES:
        if asset_type in types:
            if kind == "image" and grouped_images:
                return "image_carousel"
            return kind
    return None


def _single_fixed_size(fmt: Any) -> tuple[int, int] | None:
    sizes: set[tuple[int, int]] = set()
    for render in _attr(fmt, "renders") or []:
        dims = render.get("dimensions") if isinstance(render, Mapping) else getattr(render, "dimensions", None)
        width = dims.get("width") if isinstance(dims, Mapping) else getattr(dims, "width", None)
        height = dims.get("height") if isinstance(dims, Mapping) else getattr(dims, "height", None)
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            sizes.add((width, height))
    for asset in _asset_entries(fmt):
        width = asset.get("width") if isinstance(asset, Mapping) else getattr(asset, "width", None)
        height = asset.get("height") if isinstance(asset, Mapping) else getattr(asset, "height", None)
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            sizes.add((width, height))
    return next(iter(sizes)) if len(sizes) == 1 else None


def canonical_body_for_format(fmt: Any, ref: LegacyFormatId) -> dict[str, Any] | None:
    """Build the canonical ``{format_kind, params}`` body for one of our formats.

    Prefers the SDK ``canonical`` annotation the reference catalog ships on
    most standard formats; falls back to asset-type inference, then to the
    format id's naming convention. Returns ``None`` when no confident kind can
    be derived (``custom`` needs a ``format_shape`` we do not have). Dimensions
    and duration come from the parameterised legacy tuple first, then from the
    format's single fixed render size, then from a ``<w>x<h>`` token in the id.
    """
    params: dict[str, Any] = {}
    kind: str | None = None
    annotation = _attr(fmt, "canonical")
    if annotation is not None:
        raw_kind = _attr(annotation, "kind")
        kind = getattr(raw_kind, "value", raw_kind)
        asset_source = _attr(annotation, "asset_source")
        if asset_source is not None:
            params["asset_source"] = getattr(asset_source, "value", asset_source)
        slots = _attr(annotation, "slots_override")
        if slots:
            params["slots"] = [
                s.model_dump(mode="json", exclude_none=True) if hasattr(s, "model_dump") else s for s in slots
            ]
    if not isinstance(kind, str) or not kind or kind == "custom":
        kind = _infer_kind_from_assets(fmt) or _kind_from_id(ref.id)
    if kind is None:
        return None

    if ref.width is not None and ref.height is not None:
        params["width"], params["height"] = int(ref.width), int(ref.height)
    else:
        size = _single_fixed_size(fmt)
        if size is None:
            match = _ID_SIZE.search(ref.id)
            if match:
                size = (int(match.group(1)), int(match.group(2)))
        if size is not None:
            params["width"], params["height"] = size
    if ref.duration_ms is not None:
        duration = float(ref.duration_ms)
        params["duration_ms_exact"] = int(duration) if duration.is_integer() else duration
    return {"format_kind": kind, "params": params}


def legacy_format_converter(context: LegacyFormatConversionContext) -> Mapping[str, Any] | None:
    """SDK hook: project a named format the bundled AAO catalog cannot resolve.

    Returns ``None`` for anything we cannot describe confidently; the SDK then
    falls through to its own rules (historical display sizes, etc.) and, failing
    those, rejects the request with a precise diagnostic. Raising here would
    surface as ``custom_converter_failed`` and mask that fallback.
    """
    fmt = _salesagent_format_for(context.format_id, _current_tenant_id())
    if fmt is None or not isinstance(fmt, (BaseModel, Mapping)):
        # Unknown to every catalog we can reach (e.g. a creative agent that is
        # offline): fall back to the id's naming convention alone, the same
        # rule the SDK applies to ``display_<w>x<h>`` in the AAO namespace.
        fmt = {"id": context.format_id.id}
    try:
        return canonical_body_for_format(fmt, context.format_id)
    except Exception as exc:  # defensive: catalog entries come from remote creative agents
        logger.warning(
            "canonical-formats: could not describe %s/%s: %s", context.format_id.agent_url, context.format_id.id, exc
        )
        return None


# ---------------------------------------------------------------------------
# Option-id index and the SDK's ``canonical_format_legacy_resolver``
# ---------------------------------------------------------------------------


def _agent_url_spellings(ref: LegacyFormatId) -> list[LegacyFormatId]:
    """Return ``ref`` with and without a trailing slash on ``agent_url``."""
    url = str(ref.agent_url)
    variants = {url.rstrip("/"), url.rstrip("/") + "/"}
    variants.discard(url)
    return [ref.model_copy(update={"agent_url": v}) for v in sorted(variants)]


class _FormatOptionIndex:
    """Deterministic ``format_option_id`` → legacy tuple index (process-wide)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_option_id: dict[str, LegacyFormatId] = {}
        self._catalog_loaded_for: set[str | None] = set()

    def register(self, ref: LegacyFormatId, option_id: str | None = None) -> str:
        """Index ``ref`` under its option id.

        The SDK hashes the *raw* ``agent_url`` string, so ``…org`` and ``…org/``
        yield different ids for the same format. Stored products and buyer
        requests use either spelling; index both so lookups never depend on it.
        """
        option_id = option_id or migrated_format_option_id(ref)
        with self._lock:
            self._by_option_id.setdefault(option_id, ref)
            for variant in _agent_url_spellings(ref):
                self._by_option_id.setdefault(migrated_format_option_id(variant), variant)
        return option_id

    def get(self, option_id: str) -> LegacyFormatId | None:
        with self._lock:
            return self._by_option_id.get(option_id)

    def ensure_catalog(self, tenant_id: str | None) -> None:
        """Index every format we can enumerate for ``tenant_id`` (once per process)."""
        with self._lock:
            if tenant_id in self._catalog_loaded_for:
                return
            self._catalog_loaded_for.add(tenant_id)
        from src.core.standard_formats import get_standard_formats

        formats: list[Any] = list(get_standard_formats())
        if tenant_id is not None:
            try:
                from src.core.format_resolver import list_available_formats

                formats.extend(list_available_formats(tenant_id=tenant_id))
            except Exception as exc:  # creative agents unreachable: standard catalog still indexed
                logger.warning("canonical-formats: could not enumerate tenant %s formats: %s", tenant_id, exc)
        for fmt in formats:
            try:
                self.register(legacy_ref(fmt.format_id))
            except Exception:  # malformed catalog entry — skip, never block a request
                continue

    def reset_for_tests(self) -> None:
        with self._lock:
            self._by_option_id.clear()
            self._catalog_loaded_for.clear()


_INDEX = _FormatOptionIndex()


def resolve_option_id(option_id: str, *, tenant_id: str | None = None) -> LegacyFormatId | None:
    """Return the legacy tuple behind a canonical ``format_option_id``."""
    ref = _INDEX.get(option_id)
    if ref is None:
        _INDEX.ensure_catalog(tenant_id if tenant_id is not None else _current_tenant_id())
        ref = _INDEX.get(option_id)
    return ref


def canonical_format_legacy_resolver(
    context: CanonicalFormatLegacyResolutionContext,
) -> Sequence[LegacyFormatId] | None:
    """SDK hook: map a canonical declaration back to its named format."""
    option_id = context.declaration.format_option_id
    if not option_id:
        return None
    ref = resolve_option_id(option_id)
    return (ref,) if ref is not None else None


def declaration_for_legacy_ref(
    value: Any,
    *,
    product_id: str | None = None,
    field: str = "format_ids",
) -> CanonicalFormat | None:
    """Project a legacy tuple to a canonical declaration and index the route."""
    try:
        ref = legacy_ref(value)
    except Exception:
        return None
    projected = project_legacy_format_id(
        ref,
        product_id=product_id or "",
        field=field,
        legacy_format_converter=legacy_format_converter,
    )
    declaration = projected.declaration
    if declaration is None:
        logger.info(
            "canonical-formats: %s/%s not projectable (%s)",
            ref.agent_url,
            ref.id,
            getattr(projected.diagnostic, "resolution_failure", "unknown"),
        )
        return None
    _INDEX.register(ref, declaration.format_option_id)
    return declaration


# ---------------------------------------------------------------------------
# Pre-validation: runs on the raw wire dict BEFORE the SDK negotiates a dialect
# ---------------------------------------------------------------------------

_LEGACY_IDENTITY_TOOLS = frozenset(
    {"get_products", "create_media_buy", "update_media_buy", "sync_creatives", "list_creatives", "get_media_buys"}
)


def _walk_legacy_refs(value: Any) -> list[Any]:
    """Collect every ``format_id`` / ``format_ids`` value in a wire dict."""
    found: list[Any] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"context", "ext"}:
                continue
            if key == "format_id" and child is not None:
                found.append(child)
            elif key == "format_ids" and isinstance(child, list):
                found.extend(child)
            else:
                found.extend(_walk_legacy_refs(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_walk_legacy_refs(child))
    return found


def prepare_legacy_request(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Pre-validation hook body for legacy creative identity on the wire.

    Runs before the SDK detects the wire version, so it can:

    * remember every legacy tuple the buyer sent, so the deterministic option
      ids the SDK mints for them resolve back in :func:`legacy_request_payload`
      even for formats absent from our catalog;
    * default a version-less request that carries legacy identity to AdCP 3.0.
      MCP already does this; A2A treats an omitted version as the current
      canonical shape and would reject ``format_id`` outright, breaking every
      pre-3.1 A2A buyer. The buyer's own evidence is the tie-breaker, exactly
      as the SDK does for 3.1 requests without a capability declaration.
    """
    if tool_name not in _LEGACY_IDENTITY_TOOLS or not isinstance(params, Mapping):
        return dict(params) if isinstance(params, Mapping) else params
    out = dict(params)
    versioned = bool(out.get("adcp_version") or out.get("adcp_major_version"))
    refs = _walk_legacy_refs(params)
    if not refs:
        # No legacy identity and no version: a generic MCP client (the adcp SDK
        # clients always pin a version). The SDK would default it to 3.0 and
        # downgrade our canonical result to ``format_ids``, which the *same*
        # SDK's advertised canonical output schema then rejects on the client.
        # Treat such requests as the current 3.1 canonical shape instead.
        if not versioned:
            out["adcp_version"] = "3.1"
        return out
    for raw in refs:
        try:
            _INDEX.register(legacy_ref(raw))
        except Exception:  # malformed tuple — the SDK reports it with a field path
            continue
    if not versioned:
        out["adcp_version"] = "3.0"
    return out


# ---------------------------------------------------------------------------
# Inbound: canonical request → legacy request for the impls
# ---------------------------------------------------------------------------


def _refs_from_declarations(options: Any, *, field: str, tenant_id: str | None) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for index, option in enumerate(options or []):
        retained = getattr(option, "legacy_format_refs", None)
        if retained:
            refs.extend(_ref_wire(legacy_ref(r)) for r in retained)
            continue
        option_id = (
            option.get("format_option_id") if isinstance(option, Mapping) else getattr(option, "format_option_id", None)
        )
        resolved = resolve_option_id(option_id, tenant_id=tenant_id) if option_id else None
        if resolved is None:
            raise AdCPInvalidRequestError(
                f"{field}[{index}] references an unknown format option {option_id!r}; "
                "use a format_option_id from this seller's get_products response",
                details={"field": f"{field}[{index}]"},
            )
        refs.append(_ref_wire(resolved))
    return refs


def _refs_from_option_refs(option_refs: Any, *, field: str, tenant_id: str | None) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for index, option_ref in enumerate(option_refs or []):
        raw = option_ref.model_dump(mode="json", exclude_none=True) if hasattr(option_ref, "model_dump") else option_ref
        option_id = raw.get("format_option_id") if isinstance(raw, Mapping) else None
        resolved = resolve_option_id(option_id, tenant_id=tenant_id) if isinstance(option_id, str) else None
        if resolved is None:
            raise AdCPInvalidRequestError(
                f"{field}[{index}] references an unknown format option {option_id!r}; "
                "use a format_option_id from this seller's get_products response",
                details={"field": f"{field}[{index}]"},
            )
        refs.append(_ref_wire(resolved))
    return refs


def legacy_creative_payload(creative: Any, *, field: str = "creatives", tenant_id: str | None = None) -> Any:
    """Give a canonical creative its legacy ``format_id`` back."""
    if not isinstance(creative, Mapping):
        return creative
    body = dict(creative)
    if body.get("format_id") is not None:
        body.pop("format_kind", None)
        body.pop("format_option_ref", None)
        return body
    option_ref = body.pop("format_option_ref", None)
    kind = body.pop("format_kind", None)
    body.pop("params", None)
    if option_ref is not None:
        body["format_id"] = _refs_from_option_refs(
            [option_ref], field=f"{field}.format_option_ref", tenant_id=tenant_id
        )[0]
        return body
    raise AdCPInvalidRequestError(
        f"{field} carries format_kind={getattr(kind, 'value', kind)!r} without a format_option_ref; "
        "reference one of this seller's product format options",
        details={"field": f"{field}.format_option_ref"},
    )


def _legacy_packages(packages: Any, *, field: str, tenant_id: str | None) -> Any:
    if not isinstance(packages, list):
        return packages
    out: list[Any] = []
    for index, package in enumerate(packages):
        if not isinstance(package, Mapping):
            out.append(package)
            continue
        body = dict(package)
        option_refs = body.pop("format_option_refs", None)
        body.pop("format_kind", None)
        body.pop("params", None)
        if option_refs is not None and not body.get("format_ids"):
            body["format_ids"] = _refs_from_option_refs(
                option_refs, field=f"{field}[{index}].format_option_refs", tenant_id=tenant_id
            )
        out.append(body)
    return out


def legacy_request_payload(tool_name: str, body: dict[str, Any], *, tenant_id: str | None = None) -> dict[str, Any]:
    """Translate the canonical request body the SDK hands us into legacy shape.

    ``body`` is the dumped request (``exclude_none``); canonical-only keys are
    replaced by their legacy equivalents so the strict impl-local request
    models validate unchanged.
    """
    body = dict(body)
    if tool_name in {"get_products", "list_creatives"}:
        filters = body.get("filters")
        if isinstance(filters, Mapping) and "format_options" in filters:
            filters = dict(filters)
            options = filters.pop("format_options")
            refs = _refs_from_declarations(options, field="filters.format_options", tenant_id=tenant_id)
            if refs:
                filters["format_ids"] = refs
            body["filters"] = filters
    if tool_name == "get_products":
        fields = body.get("fields")
        if isinstance(fields, list):
            body["fields"] = list(dict.fromkeys("format_ids" if f == "format_options" else f for f in fields))
    elif tool_name in {"create_media_buy", "update_media_buy"}:
        for key in ("packages", "new_packages"):
            if key in body:
                body[key] = _legacy_packages(body[key], field=key, tenant_id=tenant_id)
    elif tool_name == "sync_creatives":
        creatives = body.get("creatives")
        if isinstance(creatives, list):
            body["creatives"] = [
                legacy_creative_payload(c, field=f"creatives[{i}]", tenant_id=tenant_id)
                for i, c in enumerate(creatives)
            ]
    return body


# ---------------------------------------------------------------------------
# Outbound: legacy wire dict → canonical wire dict for the framework
# ---------------------------------------------------------------------------


def _declaration_wire(declaration: CanonicalFormat) -> dict[str, Any]:
    # Canonical ``Format.model_dump`` strips legacy identity (``v1_format_ref``)
    # so the wire carries only ``format_option_id`` / ``format_kind`` / ``params``.
    return declaration.model_dump(mode="json", exclude_none=True)


def _canonicalize_product(product: Any) -> Any:
    if not isinstance(product, Mapping):
        return product
    body = dict(product)
    product_id = body.get("product_id") if isinstance(body.get("product_id"), str) else None
    format_ids = body.get("format_ids")
    if isinstance(format_ids, list) and format_ids and not body.get("format_options"):
        declarations = [declaration_for_legacy_ref(fid, product_id=product_id) for fid in format_ids]
        if all(d is not None for d in declarations):
            body["format_options"] = [_declaration_wire(d) for d in declarations if d is not None]
            body.pop("format_ids", None)
        # else: at least one named format has no canonical projection — the
        # spec says the product then ships legacy-only, so keep ``format_ids``.
    placements = body.get("placements")
    if isinstance(placements, list):
        body["placements"] = [_canonicalize_product(p) for p in placements]
    return body


def _canonicalize_package(package: Any) -> Any:
    if not isinstance(package, Mapping):
        return package
    body = dict(package)
    product_id = body.get("product_id") if isinstance(body.get("product_id"), str) else None
    format_ids = body.get("format_ids")
    if isinstance(format_ids, list) and not body.get("format_option_refs"):
        option_refs: list[dict[str, Any]] = []
        for fid in format_ids:
            declaration = declaration_for_legacy_ref(fid, product_id=product_id, field="format_ids")
            if declaration is None or not declaration.format_option_id:
                option_refs = []
                break
            option_refs.append({"scope": "product", "format_option_id": declaration.format_option_id})
        if option_refs or not format_ids:
            body["format_option_refs"] = option_refs
            body.pop("format_ids", None)
            # Creative requirements are implied by the referenced options.
            body.pop("format_ids_to_provide", None)
    return body


def _canonicalize_creative(creative: Any, *, publisher_domain: str) -> Any:
    if not isinstance(creative, Mapping):
        return creative
    body = dict(creative)
    format_id = body.get("format_id")
    if format_id is None or body.get("format_kind") is not None:
        return body
    declaration = declaration_for_legacy_ref(format_id, field="format_id")
    if declaration is None or not declaration.format_option_id:
        return body
    body.pop("format_id", None)
    body["format_kind"] = declaration.format_kind.value
    body["format_option_ref"] = {
        "scope": "publisher",
        "publisher_domain": publisher_domain,
        "format_option_id": declaration.format_option_id,
    }
    return body


def canonicalize_wire_response(tool_name: str, wire: dict[str, Any], *, tenant_id: str | None = None) -> dict[str, Any]:
    """Rewrite legacy format identity in an impl's wire dict to canonical shape.

    Applied at the platform boundary for every creative-boundary tool. Only
    the identity fields change; unknown / unprojectable formats keep their
    legacy shape so the SDK's legacy projection can still serve 3.0 buyers.
    """
    if not isinstance(wire, dict):
        return wire
    out = dict(wire)
    for key in _PRODUCT_COLLECTIONS:
        if isinstance(out.get(key), list):
            out[key] = [_canonicalize_product(p) for p in out[key]]
    for key in _PACKAGE_COLLECTIONS:
        if isinstance(out.get(key), list):
            out[key] = [_canonicalize_package(p) for p in out[key]]
    media_buys = out.get("media_buys")
    if isinstance(media_buys, list):
        rewritten = []
        for media_buy in media_buys:
            if isinstance(media_buy, Mapping) and isinstance(media_buy.get("packages"), list):
                media_buy = {**media_buy, "packages": [_canonicalize_package(p) for p in media_buy["packages"]]}
            rewritten.append(media_buy)
        out["media_buys"] = rewritten
    creatives = out.get("creatives")
    if isinstance(creatives, list) and tool_name in {"list_creatives", "sync_creatives"}:
        domain = _publisher_domain(tenant_id)
        out["creatives"] = [_canonicalize_creative(c, publisher_domain=domain) for c in creatives]
    return out


def reset_for_tests() -> None:
    _INDEX.reset_for_tests()


__all__ = [
    "CanonicalCreativeFeatures",
    "canonical_body_for_format",
    "canonical_format_legacy_resolver",
    "canonicalize_wire_response",
    "declaration_for_legacy_ref",
    "legacy_creative_payload",
    "legacy_format_converter",
    "legacy_ref",
    "legacy_request_payload",
    "prepare_legacy_request",
    "reset_for_tests",
    "resolve_option_id",
]
