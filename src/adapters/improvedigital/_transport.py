"""HTTP transport for the Improve Digital 360Yield Marketplace API.

Knows about OAuth2 bearer auth, JSON handling, and HTTP status -> exception
mapping. Does not know about pagination, entity shapes, or specific
endpoints — those live in :mod:`client`.

Authentication (per the Client Authentication doc):

- **OAuth2 client_credentials only.** POST ``{base_url}/oauth/token`` with
  ``grant_type=client_credentials`` and HTTP Basic auth
  (``client_id:client_secret``). The response carries the bearer under
  ``value`` (NOT ``access_token``) with a TTL in ``expiresIn`` — observed
  lifetime is ~11 minutes. There is **no refresh token**: on expiry the API
  returns 401 and the transport must mint a fresh bearer (handled by the
  refresh-on-401 retry in :meth:`_request`).

Errors arrive as an ``ExceptionWrapper`` JSON envelope
(``{"type": ..., "messages": [{"errorCode", "property", "description"}]}``)
for 400 validation failures; 401 means token expired, 403 insufficient
permissions.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import requests

from src.adapters._logging import safe_upstream_body_excerpt
from src.adapters._token_cache import BearerTokenCache

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.360yield.com"
DEFAULT_TIMEOUT = 30.0

# 360Yield bearers live ~11 minutes; a 2-minute leeway re-mints comfortably
# before expiry. (BearerTokenCache caps leeway at ttl/2, so even shorter
# observed TTLs stay usable.)
_REFRESH_LEEWAY_SECONDS = 2 * 60

# The platform rate-limits reads to 100 per 60s (429 RateLimitException).
# Pagination sweeps (inventory sync) legitimately exhaust the window, so a
# 429 is retried after sleeping; the second delay crosses a full window even
# when the quota was burned at its very start.
_RATE_LIMIT_RETRY_DELAYS = (20.0, 65.0)
_RATE_LIMIT_SLEEP_SECONDS = 61.0


class ImproveDigitalError(Exception):
    """Base exception for Improve Digital API errors.

    Carries the HTTP status code and raw response body so callers can
    inspect them without re-reading the response.
    """

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ImproveDigitalAuthError(ImproveDigitalError):
    """401 — the bearer token is invalid or expired (no refresh token exists)."""


class ImproveDigitalForbiddenError(ImproveDigitalError):
    """403 — the bearer is valid but lacks permissions for this resource."""


class ImproveDigitalNotFoundError(ImproveDigitalError):
    """404 — the requested resource does not exist."""


class ImproveDigitalRateLimitError(ImproveDigitalError):
    """429 — read quota exhausted (100 requests per 60s) after retries."""


class ImproveDigitalValidationError(ImproveDigitalError):
    """4xx (other than 401/403/404/429) — typically an ``ExceptionWrapper`` validation envelope."""


class ImproveDigitalServerError(ImproveDigitalError):
    """5xx — 360Yield's side is unhappy."""


def _exception_wrapper_summary(body: str) -> str | None:
    """Extract a compact human-readable summary from an ``ExceptionWrapper`` body.

    Returns ``None`` when the body is not the expected envelope so callers
    fall back to the generic HTTP-status message.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    messages = parsed.get("messages")
    if not isinstance(messages, list):
        return None
    parts = []
    for message in messages[:5]:
        if not isinstance(message, dict):
            continue
        prop = message.get("property")
        description = message.get("description") or message.get("errorCode")
        parts.append(f"{prop}: {description}" if prop else str(description))
    return "; ".join(parts) or None


class ImproveDigitalTransport:
    """Low-level HTTP layer for the 360Yield Marketplace API.

    Construct with ``client_id`` + ``client_secret``. The transport mints a
    bearer at ``{base_url}/oauth/token``, caches it with TTL tracking, and
    re-mints on 401 or expiry (360Yield issues no refresh tokens).
    """

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ):
        if not (client_id and client_secret):
            raise ValueError("ImproveDigitalTransport requires client_id + client_secret")

        self._client_id = client_id
        self._client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()
        self._token_cache = BearerTokenCache(
            mint_fn=self._mint_token,
            refresh_leeway_seconds=_REFRESH_LEEWAY_SECONDS,
        )

    # ----- public methods -----

    def get_json(self, path: str, **params: Any) -> Any:
        """GET a JSON resource. Returns the parsed body (dict or list)."""
        response = self._request("GET", path, params=params or None)
        return response.json() if response.content else {}

    def post_json(self, path: str, json_body: dict[str, Any] | None = None, **params: Any) -> Any:
        """POST a JSON body, parse the JSON response."""
        response = self._request(
            "POST",
            path,
            params=params or None,
            body=json.dumps(json_body) if json_body is not None else None,
            content_type="application/json",
        )
        return response.json() if response.content else {}

    def post_multipart(self, path: str, json_body: dict[str, Any], part_name: str = "body", **params: Any) -> Any:
        """POST a JSON payload as one part of a multipart/form-data request.

        The Classic creative endpoints are multipart servlets: the
        ``CreativeDto`` travels as a ``body`` part (content-type
        application/json) alongside optional binary image parts. Sending
        plain JSON gets HTTP 500 "Failed to parse multipart servlet
        request" (verified live on the dev platform).
        """
        files = {part_name: (None, json.dumps(json_body), "application/json")}
        response = self._request("POST", path, params=params or None, files=files)
        return response.json() if response.content else {}

    def put_json(self, path: str, json_body: dict[str, Any] | None = None, **params: Any) -> Any:
        """PUT a JSON body, parse the JSON response.

        Unified-deal updates require an ``update_mask`` — callers pass it via
        ``params`` (e.g. ``update_mask="budget,name"``).
        """
        response = self._request(
            "PUT",
            path,
            params=params or None,
            body=json.dumps(json_body) if json_body is not None else None,
            content_type="application/json",
        )
        return response.json() if response.content else {}

    def delete_json(self, path: str) -> None:
        """DELETE a resource. Response body (if any) is discarded."""
        self._request("DELETE", path)

    def probe(self, method: str, path: str) -> tuple[int, str]:
        """Cheap permission-check probe — return ``(status_code, body)`` without
        raising on non-2xx. Used by ``check_permissions()`` so a single 403
        on one endpoint doesn't kill the whole probe pass.

        Auth/token-mint failures still raise (the probe can't run at all
        without a valid token).
        """
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self._current_token()}", "accept": "application/json"}
        response = self._session.request(method=method, url=url, headers=headers, timeout=self.timeout)
        return response.status_code, (response.text[:200] if response.text else "")

    def logout(self) -> None:
        """Best-effort ``DELETE /oauth/logout/{token}`` — invalidates the bearer
        server-side. Failures are logged, never raised (the token expires on
        its own within minutes anyway)."""
        try:
            token = self._token_cache.current()
        except (ImproveDigitalError, requests.RequestException):
            return
        try:
            self._session.delete(f"{self.base_url}/oauth/logout/{token}", timeout=self.timeout)
        except requests.RequestException:
            logger.info("Improve Digital logout failed (ignored)", exc_info=True)
        finally:
            self._token_cache.invalidate()

    # ----- internals -----

    def _current_token(self) -> str:
        """Return a valid bearer, minting/refreshing if needed."""
        return self._token_cache.current()

    def _mint_token(self) -> tuple[str, float]:
        """OAuth2 client_credentials grant: POST ``{base_url}/oauth/token``.

        Basic auth carries the credentials; the bearer comes back under
        ``value`` with its TTL in ``expiresIn`` (seconds). Returns
        ``(token, ttl_seconds)`` for :class:`BearerTokenCache`.
        """
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        url = f"{self.base_url}/oauth/token"
        try:
            response = self._session.post(
                url,
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {basic}"},
                timeout=self.timeout,
            )
        except requests.RequestException:
            logger.warning("Improve Digital token mint failed: reason=request_exception", exc_info=True)
            raise
        if not response.ok:
            logger.warning(
                "Improve Digital token mint failed: status=%s body_excerpt=%s",
                response.status_code,
                safe_upstream_body_excerpt(response.text),
            )
            raise ImproveDigitalAuthError(
                f"Improve Digital /oauth/token rejected: HTTP {response.status_code}",
                status_code=response.status_code,
                body=response.text,
            )
        body = response.json() if response.content else {}
        token = body.get("value")
        if not token:
            logger.warning(
                "Improve Digital token mint failed: status=%s reason=missing_value body_excerpt=%s",
                response.status_code,
                safe_upstream_body_excerpt(response.text),
            )
            raise ImproveDigitalAuthError(
                "Improve Digital /oauth/token response missing token 'value'",
                status_code=response.status_code,
                body=response.text,
            )
        expires_in = float(body.get("expiresIn", 100 * 60))
        logger.info("Improve Digital: minted bearer (expires_in=%s)", int(expires_in))
        return token, expires_in

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: str | None = None,
        content_type: str | None = None,
        files: dict[str, Any] | None = None,
    ) -> requests.Response:
        try:
            response = self._do_request(method, path, params, body, content_type, files)
        except requests.Timeout:
            if method != "GET":
                raise
            # Reads stall when the rate-limit window is exhausted (observed
            # live after large paginated sweeps) — idempotent, retry once
            # after the window resets.
            logger.info(
                "Improve Digital: %s %s timed out; retrying once after %.0fs",
                method,
                path,
                _RATE_LIMIT_SLEEP_SECONDS,
            )
            time.sleep(_RATE_LIMIT_SLEEP_SECONDS)
            response = self._do_request(method, path, params, body, content_type, files)
        # No refresh token exists — a 401 with a cached token means it
        # expired. Mint a fresh one and retry once before propagating.
        if response.status_code == 401:
            logger.info("Improve Digital: 401 with cached token; minting fresh and retrying")
            self._token_cache.invalidate()
            response = self._do_request(method, path, params, body, content_type)
        for delay in _RATE_LIMIT_RETRY_DELAYS:
            if response.status_code != 429:
                break
            retry_after = response.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
            logger.info(
                "Improve Digital: 429 rate-limited on %s %s — sleeping %.0fs before retry",
                method,
                path,
                wait,
            )
            time.sleep(wait)
            response = self._do_request(method, path, params, body, content_type)
        self._raise_for_status(response, method, path)
        return response

    def _do_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        body: str | None,
        content_type: str | None,
        files: dict[str, Any] | None = None,
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        headers = {
            "Authorization": f"Bearer {self._current_token()}",
            "accept": "application/json",
        }
        if content_type:
            headers["Content-Type"] = content_type
        try:
            return self._session.request(
                method=method,
                url=url,
                headers=headers,
                data=body,
                files=files,
                timeout=self.timeout,
            )
        except requests.RequestException:
            logger.warning(
                "Improve Digital API request failed: method=%s path=%s reason=request_exception",
                method,
                path,
                exc_info=True,
            )
            raise

    def _raise_for_status(self, response: requests.Response, method: str, path: str) -> None:
        if response.ok:
            return
        status = response.status_code
        body = response.text
        message = f"Improve Digital {method} {path} -> HTTP {status}"
        summary = _exception_wrapper_summary(body)
        if summary:
            message = f"{message}: {summary}"
        logger.warning(
            "Improve Digital API request failed: method=%s path=%s status=%s body_excerpt=%s",
            method,
            path,
            status,
            safe_upstream_body_excerpt(body),
        )
        if status == 401:
            raise ImproveDigitalAuthError(message, status_code=status, body=body)
        if status == 403:
            raise ImproveDigitalForbiddenError(message, status_code=status, body=body)
        if status == 404:
            raise ImproveDigitalNotFoundError(message, status_code=status, body=body)
        if status == 429:
            raise ImproveDigitalRateLimitError(message, status_code=status, body=body)
        if 400 <= status < 500:
            raise ImproveDigitalValidationError(message, status_code=status, body=body)
        raise ImproveDigitalServerError(message, status_code=status, body=body)
