"""ASGI middleware: start/stop background schedulers on lifespan events.

The SDK's ``adcp.server.serve()`` composes its own MCP+A2A lifespans and
doesn't expose a user-supplied ``lifespan`` hook, but the ``asgi_middleware``
chain does see ``lifespan.startup`` and ``lifespan.shutdown`` scope events.
This middleware intercepts those to fire scheduler start/stop coroutines
on the running event loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


class SchedulerLifespanMiddleware:
    """Run async start/stop hooks aligned to ASGI lifespan events.

    ``startups`` runs after ``lifespan.startup`` is received, before the
    response is sent downstream. ``shutdowns`` runs on ``lifespan.shutdown``.
    Failures are logged but never block the lifespan handshake — a
    misbehaving scheduler must not stop the server from serving.
    """

    def __init__(
        self,
        app: Any,
        startups: list[Callable[[], Awaitable[None]]] | None = None,
        shutdowns: list[Callable[[], Awaitable[None]]] | None = None,
    ) -> None:
        self.app = app
        self.startups = startups or []
        self.shutdowns = shutdowns or []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return

        async def wrapped_receive() -> dict[str, Any]:
            message = await receive()
            if message["type"] == "lifespan.startup":
                for hook in self.startups:
                    try:
                        await hook()
                    except Exception:
                        logger.exception("Scheduler startup hook failed")
            elif message["type"] == "lifespan.shutdown":
                for hook in self.shutdowns:
                    try:
                        await asyncio.wait_for(hook(), timeout=10.0)
                    except Exception:
                        logger.exception("Scheduler shutdown hook failed")
            return message

        await self.app(scope, wrapped_receive, send)
