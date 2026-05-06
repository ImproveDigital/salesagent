"""Salesagent-side signing infrastructure.

PR 1 of [signing-non-embedded](../../../../docs/design/signing-non-embedded.md):
this module is the local glue around the ``adcp.signing`` library. The library
owns the protocol-correct primitives (RFC 9421 verifier, brand.json resolver,
PgReplayStore, SigningProvider). We own:

* ``OperatorBrandJsonCache`` — process-singleton cache of
  :class:`adcp.signing.BrandJsonJwksResolver` keyed on ``(brand_json_url, agent_type)``.
  Concurrent verifies on the same operator share one cached brand.json snapshot.
* ``replay_store`` — :class:`adcp.signing.PgReplayStore` bootstrap + sweep
  mode detection (``pg_cron`` if present, in-process timer otherwise).

Middleware mount + admin UI come in PRs 2 and 3.
"""

from __future__ import annotations

from src.core.signing.middleware import SigningVerifyMiddleware
from src.core.signing.operator_brand_json_cache import (
    OperatorBrandJsonCache,
    get_operator_brand_json_cache,
)
from src.core.signing.replay_store import (
    bootstrap_replay_store,
    get_replay_store,
)
from src.core.signing.verified_state import (
    VerifiedRequestState,
    clear_verified_state,
    get_verified_state,
    set_verified_state,
)

__all__ = [
    "OperatorBrandJsonCache",
    "SigningVerifyMiddleware",
    "VerifiedRequestState",
    "bootstrap_replay_store",
    "clear_verified_state",
    "get_operator_brand_json_cache",
    "get_replay_store",
    "get_verified_state",
    "set_verified_state",
]
