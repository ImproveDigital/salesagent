"""Lightweight models for 360Yield Marketplace API entities (Classic surface).

Only the fields the adapter actually reads are declared; everything else
passes through untyped (``extra="allow"``) so upstream additions never break
parsing. Grows alongside the client during the M1/M2 milestones.

Wire reference: ``docs/adapters/improvedigital/api-doc/rtb-v3-openapi.json``
(``CampaignDto``, ``CommonDealLineItemDto``, ``CreativeDto``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _UpstreamModel(BaseModel):
    """Base for upstream entities — tolerant of unknown fields."""

    model_config = ConfigDict(extra="allow")


class ClassicCampaign(_UpstreamModel):
    """A Classic (direct) campaign — ``GET /rtb/v1/classic/campaigns/{id}``."""

    id: int | None = None
    name: str | None = None
    status: str | None = None
    active: bool | None = None
    start_date: str | None = None
    end_date: str | None = None
    budget: float | None = None
    currency: str | None = None
    advertiserId: int | None = None
    agencyId: int | None = None
    improve_demand_contact_id: int | None = None


class ClassicLineItem(_UpstreamModel):
    """A Classic line item — ``GET /rtb/v1/classic/campaigns/{cid}/line-items/{id}``."""

    id: int | None = None
    campaign_id: int | None = None
    name: str | None = None
    active: bool | None = None
    line_item_status: str | None = None
    line_item_state: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    budget: float | None = None
    currency: str | None = None
    pricing_model: str | None = None
    cpm_bid: float | None = None
    impression_cap: int | None = None


class ClassicCreative(_UpstreamModel):
    """A Classic creative — ``GET /rtb/v1/classic/campaigns/{cid}/creatives/{id}``."""

    id: int | None = None
    campaign_id: int | None = None
    name: str | None = None
    type: str | None = None
    size: str | None = None
    status: str | None = None
    tag: str | None = None
    image_url: str | None = None
    image_click_url: str | None = None


class PaginatedResponse(_UpstreamModel):
    """Standard list envelope: ``content`` + pagination metadata."""

    content: list[dict[str, Any]] = Field(default_factory=list)
    totalNumberOfElemements: int | None = None  # (sic — upstream spelling)
    contentRange: str | None = None
