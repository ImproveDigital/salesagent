"""Canonical creative formats supported by the Improve Digital adapter.

Classic campaigns host creatives on the Improve adserver: banner tags/images,
VAST video, native, and audio (``CreativeDto`` types). This module declares
the canonical reference-agent formats those map onto; per-size display
variants come later from the synced size lookups (``/rtb/v1/sizes-all``,
``/rtb/v1/classic/creative-types/{type}/sizes``).

Format definitions come from the adcp SDK's bundled reference catalog
(``adcp.canonical_formats.fixtures``); ``audio_vast`` is not yet in the SDK
catalog, so it is supplemented locally with the same reference-agent URL.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from adcp.canonical_formats.fixtures import load_v1_reference_catalog

from src.core.schemas import Format

IMPROVEDIGITAL_CANONICAL_FORMAT_IDS = (
    "display_image",
    "display_html",
    "display_js",
    "video_vast",
    "native_standard",
    "audio_vast",
)


def _audio_vast_raw(agent_url: str) -> dict[str, Any]:
    """Canonical VAST audio format — supplements the SDK catalog until it lands there."""
    return {
        "format_id": {"agent_url": agent_url, "id": "audio_vast"},
        "name": "VAST Audio",
        "type": "audio",
        "description": "Audio ad via VAST tag (supports any duration)",
        "accepts_parameters": ["duration"],
        "assets": [
            {
                "item_type": "individual",
                "asset_id": "vast_tag",
                "asset_type": "vast",
                "required": True,
            }
        ],
    }


@cache
def _canonical_formats() -> tuple[Format, ...]:
    """Load and validate the adapter's canonical formats once per process."""
    catalog = {entry["format_id"]["id"]: entry for entry in load_v1_reference_catalog()}
    # Keep the supplement's agent_url identical to the catalog's so all six
    # formats dedupe/compare consistently downstream.
    agent_url = next(iter(catalog.values()))["format_id"]["agent_url"]
    formats: list[Format] = []
    for format_id in IMPROVEDIGITAL_CANONICAL_FORMAT_IDS:
        raw = catalog.get(format_id) or (_audio_vast_raw(agent_url) if format_id == "audio_vast" else None)
        if raw is not None:
            formats.append(Format.model_validate(raw))
    return tuple(formats)


def improvedigital_creative_format_models() -> list[Format]:
    """Return Improve Digital-supported canonical reference-agent formats."""
    return [fmt.model_copy(deep=True) for fmt in _canonical_formats()]


def improvedigital_creative_formats(tenant_id: str | None = None) -> list[dict[str, Any]]:
    """Return Improve Digital-supported canonical formats as AdCP Format dicts.

    ``tenant_id`` is accepted for interface conformance; declarations are
    tenant-independent.
    """
    return [fmt.model_dump(mode="json") for fmt in improvedigital_creative_format_models()]
