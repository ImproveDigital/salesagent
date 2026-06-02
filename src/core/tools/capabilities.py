"""Capabilities-response constants.

The actual ``get_adcp_capabilities`` wire response is built by the SDK's
:meth:`adcp.decisioning.handler.PlatformHandler.get_adcp_capabilities`
projection of :class:`DecisioningCapabilities` declared in
``core/main.py:build_router``. There is no per-tenant impl on this side
of the boundary anymore — the framework owns the response shape.

What survives here is the constants the router declaration consumes.
"""

#: Idempotency replay window advertised in capabilities. 24h matches the
#: PgBackend's default cache window. Sourced here as a constant rather than
#: re-derived in each capability response — and re-imported by
#: ``core/main.py`` so the router declaration and the dedup window stay
#: in lock-step.
IDEMPOTENCY_REPLAY_TTL_SECONDS = 86400
