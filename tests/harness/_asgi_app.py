"""Process-wide ASGI app + dedicated event loop for in-process MCP dispatch.

Test harnesses dispatch MCP requests through the same Starlette app
production runs (built by :func:`core.main.build_app`). The app's
session manager (FastMCP streamable-http) can only run once per
instance, so we keep one app per process and one event loop that
owns both its lifespan and every request dispatched against it.

Why a dedicated loop instead of pytest-asyncio's per-test loop:

* FastMCP's :class:`StreamableHTTPSessionManager` raises
  ``RuntimeError: .run() can only be called once`` if you stop and
  restart its lifespan — fine for production, fatal for any test
  pattern that drives lifespan.startup more than once on the same
  app instance.
* anyio binds the async backend at startup time. Running requests
  on a different loop than the lifespan corrupts the session
  manager's task group.

Solution: one daemon thread runs an asyncio loop for the whole
process. The lifespan starts on first MCP call and runs until the
process exits. Every request is dispatched onto that same loop via
:func:`asyncio.run_coroutine_threadsafe`, regardless of which loop
the calling test happens to be on.

The auth chain runs through the bearer-token middleware: tests that
want full-pipeline auth pass a real ``x-adcp-auth`` token. Unit-mode
tests patch ``resolve_identity_from_context`` at the wrapper layer;
the bearer middleware accepts the unknown token (returns ``None``)
and the patched resolver overrides identity downstream.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from typing import Any

_APP: Any = None
_LOOP: asyncio.AbstractEventLoop | None = None
_THREAD: threading.Thread | None = None
_LIFESPAN_TASK: asyncio.Task | None = None
_LIFESPAN_RECEIVE: asyncio.Queue | None = None
_LIFESPAN_SEND: asyncio.Queue | None = None
_LOCK = threading.Lock()


def _start_loop() -> asyncio.AbstractEventLoop:
    """Spin up the dedicated event loop on a daemon thread."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=loop.run_forever,
        name="adcp-test-asgi",
        daemon=True,
    )
    thread.start()

    global _THREAD
    _THREAD = thread
    return loop


async def _start_lifespan(app: Any) -> tuple[asyncio.Task, asyncio.Queue, asyncio.Queue]:
    """Send ``lifespan.startup`` on the running loop and return the task."""
    receive_q: asyncio.Queue = asyncio.Queue()
    send_q: asyncio.Queue = asyncio.Queue()

    async def receive() -> dict:
        return await receive_q.get()

    async def send(msg: dict) -> None:
        await send_q.put(msg)

    task = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await receive_q.put({"type": "lifespan.startup"})
    msg = await send_q.get()
    if msg["type"] != "lifespan.startup.complete":
        raise RuntimeError(f"ASGI lifespan startup failed: {msg}")
    return task, receive_q, send_q


async def _close_pools_on_this_loop() -> None:
    """Close psycopg pools whose worker tasks were bound to this loop.

    The idempotency ``PgBackend`` opens its pools lazily on the first async
    call, i.e. on this loop (see ``core.idempotency._LazyBootstrapPgBackend``).
    ``AsyncConnectionPool.close()`` must run on the owning loop; doing it
    here, while the loop is still alive, is the only place that works.
    Without it the loop stops with the pool workers pending and asyncio
    prints ``Task was destroyed but it is pending!`` at interpreter exit.
    """
    try:
        from core import idempotency
    except Exception:  # pragma: no cover - core is always importable in-process
        return
    for name in ("_POOL", "_LOCK_POOL"):
        pool = getattr(idempotency, name, None)
        if pool is None:
            continue
        try:
            await asyncio.wait_for(pool.close(), timeout=5)
        except Exception:
            pass


def _shutdown() -> None:
    """Orderly lifespan shutdown, pool close, task cancel and loop stop.

    Idempotent. Runs from :func:`pytest_sessionfinish` (root ``conftest``) so
    the teardown logs land in pytest's live capture; the ``atexit`` hook is
    only the fallback for non-pytest callers.
    """
    global _LIFESPAN_TASK, _LIFESPAN_RECEIVE, _LIFESPAN_SEND, _LOOP, _THREAD
    loop = _LOOP
    if loop is None or not loop.is_running():
        return

    async def _stop() -> None:
        if _LIFESPAN_RECEIVE is not None:
            await _LIFESPAN_RECEIVE.put({"type": "lifespan.shutdown"})
        if _LIFESPAN_SEND is not None:
            try:
                await asyncio.wait_for(_LIFESPAN_SEND.get(), timeout=5)
            except TimeoutError:
                pass
        if _LIFESPAN_TASK is not None:
            _LIFESPAN_TASK.cancel()
            try:
                await _LIFESPAN_TASK
            except (asyncio.CancelledError, Exception):
                pass
        await _close_pools_on_this_loop()
        # Anything still scheduled on this loop (pool workers that outlived
        # close(), session-manager helpers) is cancelled while the loop can
        # still run their cleanup, instead of being destroyed pending.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    try:
        asyncio.run_coroutine_threadsafe(_stop(), loop).result(timeout=15)
    except Exception:
        pass
    loop.call_soon_threadsafe(loop.stop)
    if _THREAD is not None:
        _THREAD.join(timeout=5)
    _LOOP = None
    _LIFESPAN_TASK = _LIFESPAN_RECEIVE = _LIFESPAN_SEND = None


def shutdown_app_loop() -> None:
    """Public entry point for the pytest session-finish hook."""
    _shutdown()


def _ensure_started() -> tuple[Any, asyncio.AbstractEventLoop]:
    """Build the app + start the lifespan + register atexit teardown."""
    global _APP, _LOOP, _LIFESPAN_TASK, _LIFESPAN_RECEIVE, _LIFESPAN_SEND
    with _LOCK:
        if _APP is not None and _LOOP is not None:
            return _APP, _LOOP

        from core.main import build_app

        app = build_app()
        loop = _start_loop()
        task, recv, snd = asyncio.run_coroutine_threadsafe(_start_lifespan(app), loop).result(timeout=60)

        _APP = app
        _LOOP = loop
        _LIFESPAN_TASK = task
        _LIFESPAN_RECEIVE = recv
        _LIFESPAN_SEND = snd
        atexit.register(_shutdown)
        return app, loop


def run_on_app_loop(coro_factory):
    """Run an async callable on the ASGI app's dedicated loop.

    ``coro_factory`` is a zero-arg callable that returns a coroutine —
    the coroutine MUST be created inside the dedicated loop's thread,
    because ``asyncio.AsyncClient`` and ``ASGITransport`` bind to the
    creating loop at instantiation. The factory pattern lets us defer
    creation until we're on the right loop.
    """
    app, loop = _ensure_started()

    async def _run():
        coro = coro_factory(app)
        return await coro

    return asyncio.run_coroutine_threadsafe(_run(), loop).result()
