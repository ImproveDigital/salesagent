"""Improve Digital (Azerion 360Yield Marketplace) ad-server adapter.

Phase 1 targets **Classic (direct) campaigns** — creatives hosted and served
by the Improve adserver — at ``https://api.360yield.com``:

- OAuth2 client_credentials auth (``/oauth/token`` — short-lived bearer, no
  refresh token, re-mint on 401)
- Classic campaigns + line items + creatives (``/rtb/v1/classic/*``)
- Placement search (``/rtb/v3/placements``) for inventory selection
- Improve Marketplace Report API (``/report``) for definitive delivery

See ``docs/adapters/improvedigital/INTEGRATION_PLAN.md`` for the phased
integration plan and the committed OpenAPI spec under
``docs/adapters/improvedigital/api-doc/``.
"""

from .adapter import ImproveDigitalAdapter
from .client import (
    ImproveDigitalAuthError,
    ImproveDigitalClient,
    ImproveDigitalError,
    ImproveDigitalForbiddenError,
    ImproveDigitalNotFoundError,
    ImproveDigitalServerError,
    ImproveDigitalValidationError,
)
from .schemas import ImproveDigitalConnectionConfig, ImproveDigitalProductConfig

__all__ = [
    "ImproveDigitalAdapter",
    "ImproveDigitalAuthError",
    "ImproveDigitalClient",
    "ImproveDigitalConnectionConfig",
    "ImproveDigitalError",
    "ImproveDigitalForbiddenError",
    "ImproveDigitalNotFoundError",
    "ImproveDigitalProductConfig",
    "ImproveDigitalServerError",
    "ImproveDigitalValidationError",
]
