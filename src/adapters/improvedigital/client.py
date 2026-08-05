"""Public API client for the Improve Digital 360Yield Marketplace API.

Phase 1 targets **Classic (direct) campaigns** — campaigns whose creatives
are hosted and served by the Improve adserver — via the ``/rtb/v1/classic/*``
surface. Composes per-resource sub-clients over a shared OAuth2 transport:

- ``client.campaigns`` — Classic campaign + line-item lifecycle (CRUD,
  pause/resume, placement assignment)
- ``client.creatives`` — Classic creative CRUD, status, line-item
  assignment, VAST validation
- ``client.inventory`` — buy-side placement search + packages
- ``client.lookups``   — dimension lookups (sizes, geo, creative types)
- ``client.reporting`` — Report API (preview / async generation / status)

Endpoint paths come from the committed OpenAPI spec
(``docs/adapters/improvedigital/api-doc/rtb-v3-openapi.json``) plus the
Report API Confluence doc. Sub-clients grow method-by-method as milestones
land; each method is a thin path+params wrapper — no business logic here.
"""

from __future__ import annotations

from typing import Any

import requests

from src.adapters.improvedigital._transport import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    ImproveDigitalAuthError,
    ImproveDigitalError,
    ImproveDigitalForbiddenError,
    ImproveDigitalNotFoundError,
    ImproveDigitalServerError,
    ImproveDigitalTransport,
    ImproveDigitalValidationError,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "ImproveDigitalAuthError",
    "ImproveDigitalClient",
    "ImproveDigitalError",
    "ImproveDigitalForbiddenError",
    "ImproveDigitalNotFoundError",
    "ImproveDigitalServerError",
    "ImproveDigitalValidationError",
]


class ImproveDigitalCampaignsClient:
    """Classic campaign + line-item lifecycle (``/rtb/v1/classic/*``)."""

    def __init__(self, transport: ImproveDigitalTransport):
        self._transport = transport

    # -- campaigns --

    def create_campaign(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /rtb/v1/classic/campaigns`` — ``CampaignDto``; required:
        ``name``, ``start_date``, ``improve_demand_contact_id``."""
        return self._transport.post_json("/rtb/v1/classic/campaigns", payload)

    def get_campaign(self, campaign_id: int) -> dict[str, Any]:
        return self._transport.get_json(f"/rtb/v1/classic/campaigns/{campaign_id}")

    def update_campaign(self, campaign_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._transport.put_json(f"/rtb/v1/classic/campaigns/{campaign_id}", payload)

    def delete_campaign(self, campaign_id: int) -> None:
        self._transport.delete_json(f"/rtb/v1/classic/campaigns/{campaign_id}")

    def list_campaigns(self, **params: Any) -> dict[str, Any]:
        return self._transport.get_json("/rtb/v1/classic/campaigns", **params)

    def archive_campaign(self, campaign_id: int) -> None:
        self._transport.put_json(f"/rtb/v3/campaigns/{campaign_id}/archive")

    # -- line items --

    def create_line_item(self, campaign_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /rtb/v1/classic/campaigns/{id}/line-items`` —
        ``CommonDealLineItemDto``; required: ``name``, ``start_date``."""
        return self._transport.post_json(f"/rtb/v1/classic/campaigns/{campaign_id}/line-items", payload)

    def get_line_item(self, campaign_id: int, line_item_id: int) -> dict[str, Any]:
        return self._transport.get_json(f"/rtb/v1/classic/campaigns/{campaign_id}/line-items/{line_item_id}")

    def update_line_item(self, campaign_id: int, line_item_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._transport.put_json(f"/rtb/v1/classic/campaigns/{campaign_id}/line-items/{line_item_id}", payload)

    def delete_line_item(self, campaign_id: int, line_item_id: int) -> None:
        self._transport.delete_json(f"/rtb/v1/classic/campaigns/{campaign_id}/line-items/{line_item_id}")

    def list_line_items(self, campaign_id: int, **params: Any) -> dict[str, Any]:
        return self._transport.get_json(f"/rtb/v1/classic/campaigns/{campaign_id}/line-items", **params)

    def list_all_line_items(self, **params: Any) -> dict[str, Any]:
        return self._transport.get_json("/rtb/v1/classic/line-items", **params)

    def set_line_item_status(self, campaign_id: int, line_item_id: int, *, active: bool) -> None:
        """Pause (``active=false``) or resume (``active=true``) a line item."""
        self._transport.put_json(
            f"/rtb/v3/campaigns/{campaign_id}/line-items/{line_item_id}/status",
            active=active,
        )

    def archive_line_item(self, campaign_id: int, line_item_id: int) -> None:
        self._transport.put_json(f"/rtb/v3/campaigns/{campaign_id}/line-items/{line_item_id}/archive")

    # -- placement / package assignment --

    def assign_placements(self, campaign_id: int, line_item_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """``PUT /rtb/v2/classic/.../placements/assign``."""
        return self._transport.put_json(
            f"/rtb/v2/classic/campaigns/{campaign_id}/line-items/{line_item_id}/placements/assign", payload
        )

    def unassign_placements(self, campaign_id: int, line_item_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._transport.put_json(
            f"/rtb/v2/classic/campaigns/{campaign_id}/line-items/{line_item_id}/placements/unassign", payload
        )

    def list_placements(self, campaign_id: int, line_item_id: int, **params: Any) -> dict[str, Any]:
        return self._transport.get_json(
            f"/rtb/v2/classic/campaigns/{campaign_id}/line-items/{line_item_id}/placements", **params
        )

    def set_packages(self, campaign_id: int, line_item_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._transport.put_json(
            f"/rtb/v1/classic/campaigns/{campaign_id}/line-items/{line_item_id}/packages", payload
        )


class ImproveDigitalCreativesClient:
    """Classic creatives — hosted by the Improve adserver.

    Creative create is plain JSON (``CreativeDto``; required: ``name``,
    ``type``, ``size``, ``status``, ``tag``); binary asset uploads go through
    the bulk-upload endpoints (later milestone).
    """

    def __init__(self, transport: ImproveDigitalTransport):
        self._transport = transport

    def create_creative(self, campaign_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /rtb/v1/classic/campaigns/{id}/creatives``."""
        return self._transport.post_json(f"/rtb/v1/classic/campaigns/{campaign_id}/creatives", payload)

    def get_creative(self, campaign_id: int, creative_id: int) -> dict[str, Any]:
        return self._transport.get_json(f"/rtb/v1/classic/campaigns/{campaign_id}/creatives/{creative_id}")

    def update_creative(self, campaign_id: int, creative_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._transport.put_json(f"/rtb/v1/classic/campaigns/{campaign_id}/creatives/{creative_id}", payload)

    def delete_creative(self, campaign_id: int, creative_id: int) -> None:
        self._transport.delete_json(f"/rtb/v1/classic/campaigns/{campaign_id}/creatives/{creative_id}")

    def list_creatives(self, campaign_id: int, **params: Any) -> dict[str, Any]:
        return self._transport.get_json(f"/rtb/v1/classic/campaigns/{campaign_id}/creatives", **params)

    def set_creative_status(self, campaign_id: int, creative_id: int, *, activate: bool) -> None:
        self._transport.put_json(
            f"/rtb/v1/classic/campaigns/{campaign_id}/creatives/{creative_id}/status",
            activate=activate,
        )

    def set_line_item_creatives(self, campaign_id: int, line_item_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """``PUT /rtb/v1/classic/.../line-items/{id}/creatives`` — binds
        creatives (with weights/flighting) to a line item
        (``LineItemCreativesDto``)."""
        return self._transport.put_json(
            f"/rtb/v1/classic/campaigns/{campaign_id}/line-items/{line_item_id}/creatives", payload
        )

    def list_line_item_creatives(self, campaign_id: int, line_item_id: int, **params: Any) -> dict[str, Any]:
        return self._transport.get_json(
            f"/rtb/v1/classic/campaigns/{campaign_id}/line-items/{line_item_id}/creatives", **params
        )

    def unassign_all_creatives(self, campaign_id: int, line_item_id: int) -> None:
        self._transport.put_json(
            f"/rtb/v1/classic/campaigns/{campaign_id}/line-items/{line_item_id}/creatives/unassign-all"
        )

    def validate_vast_url(self, url: str) -> dict[str, Any]:
        return self._transport.get_json("/rtb/v3/classic/creatives/validate-vast-url", url=url)


class ImproveDigitalInventoryClient:
    """Buy-side placement search + reusable packages."""

    def __init__(self, transport: ImproveDigitalTransport):
        self._transport = transport

    def search_placements(self, **params: Any) -> dict[str, Any]:
        """``GET /rtb/v3/placements`` — filters include ``publisher_ids``,
        ``size_ids``, ``iab_categories``, ``seller_types``, ``azerion_owned``,
        free-text ``search``, plus ``offset``/``limit`` pagination."""
        return self._transport.get_json("/rtb/v3/placements", **params)

    def list_packages(self, **params: Any) -> dict[str, Any]:
        """``GET /rtb/v1/packages`` — reusable placement groupings."""
        return self._transport.get_json("/rtb/v1/packages", **params)


class ImproveDigitalLookupsClient:
    """Dimension lookups for targeting/config pickers."""

    def __init__(self, transport: ImproveDigitalTransport):
        self._transport = transport

    def sizes(self, **params: Any) -> Any:
        return self._transport.get_json("/rtb/v1/sizes-all", **params)

    def creative_type_sizes(self, creative_type: str, **params: Any) -> Any:
        """``GET /rtb/v1/classic/creative-types/{type}/sizes``."""
        return self._transport.get_json(f"/rtb/v1/classic/creative-types/{creative_type}/sizes", **params)

    def countries(self, **params: Any) -> Any:
        return self._transport.get_json("/common/v1/countries", **params)

    def user_details(self) -> dict[str, Any]:
        """``GET /lookup/v1/user-details`` — identity behind the OAuth pair
        (user_id, name, business unit, buyers). Lookup-scoped, so it works
        even for credentials without admin scope."""
        return self._transport.get_json("/lookup/v1/user-details")


class ImproveDigitalAdminClient:
    """Admin API discovery — buying entities and their offices.

    Requires admin-scoped credentials (403 otherwise); callers must treat
    these as best-effort and fall back to manually configured IDs.
    """

    def __init__(self, transport: ImproveDigitalTransport):
        self._transport = transport

    def list_buying_entities(self, **params: Any) -> dict[str, Any]:
        """``GET /admin/v1/buying-entities-combo`` — lightweight ``{id, name}``
        rows for pickers."""
        return self._transport.get_json("/admin/v1/buying-entities-combo", **params)

    def list_buying_entity_offices(self, buying_entity_id: int, **params: Any) -> dict[str, Any]:
        """``GET /admin/v1/buying-entities/{id}/buying-entity-offices`` — office
        rows carry ``improve_demand_contact_id``, ``billing_currency_code`` and
        ``buying_types`` (sandbox-confirmed)."""
        return self._transport.get_json(f"/admin/v1/buying-entities/{buying_entity_id}/buying-entity-offices", **params)


class ImproveDigitalReportingClient:
    """Improve Marketplace Report API (definitive delivery metrics)."""

    def __init__(self, transport: ImproveDigitalTransport):
        self._transport = transport

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /report/ext/preview`` — synchronous, ≤500 rows as JSON."""
        return self._transport.post_json("/report/ext/preview", payload)

    def submit_generation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /report/ext/generation`` — async CSV/Excel job, ≤1M rows."""
        return self._transport.post_json("/report/ext/generation", payload)

    def generation_status(self, report_generation_id: str) -> dict[str, Any]:
        return self._transport.get_json(f"/report/ext/generation-status/{report_generation_id}")

    def allowed_filters(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._transport.post_json("/report/allowed-filters", payload or {})


class ImproveDigitalClient:
    """Facade over the 360Yield Marketplace API — composes the sub-clients."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ):
        self._transport = ImproveDigitalTransport(
            client_id=client_id,
            client_secret=client_secret,
            base_url=base_url,
            timeout=timeout,
            session=session,
        )
        self.campaigns = ImproveDigitalCampaignsClient(self._transport)
        self.creatives = ImproveDigitalCreativesClient(self._transport)
        self.inventory = ImproveDigitalInventoryClient(self._transport)
        self.lookups = ImproveDigitalLookupsClient(self._transport)
        self.reporting = ImproveDigitalReportingClient(self._transport)
        self.admin = ImproveDigitalAdminClient(self._transport)

    def probe(self, method: str, path: str) -> tuple[int, str]:
        """Non-raising permission probe — see :meth:`ImproveDigitalTransport.probe`."""
        return self._transport.probe(method, path)

    def close(self) -> None:
        """Best-effort server-side token invalidation."""
        self._transport.logout()
