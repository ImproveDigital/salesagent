"""Canonical creative formats supported by the Improve Digital adapter.

Classic campaigns host creatives on the Improve adserver: banner tags/images,
VAST video, native, and audio (``CreativeDto`` types). Phase 1 declares the
canonical reference-agent formats those map onto; per-size display variants
come later from the synced size lookups (``/rtb/v1/sizes-all``,
``/rtb/v1/classic/creative-types/{type}/sizes``).
"""

from __future__ import annotations

from typing import Any

from src.core.schemas import Format
from src.core.standard_formats import get_standard_format

IMPROVEDIGITAL_CANONICAL_FORMAT_IDS = (
    "display_image",
    "display_html",
    "display_js",
    "video_vast",
    "native_standard",
    "audio_vast",
)


def improvedigital_creative_format_models() -> list[Format]:
    """Return Improve Digital-supported canonical reference-agent formats."""
    formats: list[Format] = []
    for format_id in IMPROVEDIGITAL_CANONICAL_FORMAT_IDS:
        fmt = get_standard_format(format_id)
        if fmt is not None:
            formats.append(fmt.model_copy(deep=True))
    return formats


def improvedigital_creative_formats(tenant_id: str | None = None) -> list[dict[str, Any]]:
    """Return Improve Digital-supported canonical formats as AdCP Format dicts.

    ``tenant_id`` is accepted for interface conformance; declarations are
    tenant-independent.
    """
    return [fmt.model_dump(mode="json") for fmt in improvedigital_creative_format_models()]
