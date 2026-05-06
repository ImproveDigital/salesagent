"""Bootstrap + access for the shared :class:`adcp.signing.PgReplayStore`.

PR 1 of [signing-non-embedded](../../../../docs/design/signing-non-embedded.md).

Multi-worker replay protection. The library owns the implementation; we own:

* a process-singleton ``psycopg_pool.ConnectionPool`` (we don't share with
  SQLAlchemy because the pool drivers differ — psycopg3 vs psycopg2-binary)
* a startup bootstrap that calls ``PgReplayStore.create_schema()`` (idempotent)
* sweep-mode detection: ``pg_cron`` if the extension is present in the target
  database, in-process asyncio task otherwise. Configurable via
  ``REPLAY_SWEEP_MODE`` env (``auto`` | ``pg_cron`` | ``in_process`` | ``off``)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adcp.signing import PgReplayStore
    from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_INTERVAL_SECONDS = 60.0
PG_CRON_JOB_NAME = "adcp_replay_sweep"

_pool: ConnectionPool | None = None
_store: PgReplayStore | None = None
_sweep_task: asyncio.Task[None] | None = None


def _build_dsn() -> str:
    """Construct a libpq DSN from the salesagent's database config."""
    from src.core.database.db_config import DatabaseConfig

    cfg = DatabaseConfig.get_db_config()
    if "host" in cfg:
        host = cfg.get("host", "localhost")
        port = cfg.get("port", 5432)
        return (
            f"host={host} port={port} dbname={cfg.get('database', 'adcp')} "
            f"user={cfg.get('user', 'adcp')} password={cfg.get('password', '')} "
            f"sslmode={cfg.get('sslmode', 'prefer')}"
        )
    if "host_path" in cfg:
        return (
            f"host={cfg['host_path']} dbname={cfg.get('database', 'adcp')} "
            f"user={cfg.get('user', 'adcp')} password={cfg.get('password', '')}"
        )
    raise RuntimeError("Unrecognized DatabaseConfig shape; cannot build DSN")


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    from psycopg_pool import ConnectionPool

    dsn = _build_dsn()
    _pool = ConnectionPool(dsn, min_size=1, max_size=8, open=True)
    return _pool


def get_replay_store() -> PgReplayStore:
    """Return the process-wide :class:`PgReplayStore` singleton.

    Lazy — first call constructs the pool + store. Subsequent calls return
    the same instance. Idempotent against concurrent first-callers (CPython
    GIL serializes the construction; we rely on that, not a lock, because
    replay-store construction has no observable side effects beyond pool open).
    """
    global _store
    if _store is not None:
        return _store
    from adcp.signing import PgReplayStore

    pool = _get_pool()
    _store = PgReplayStore(pool=pool)
    return _store


def _has_pg_cron(pool: ConnectionPool) -> bool:
    """Probe whether the target database has the ``pg_cron`` extension installed."""
    try:
        with pool.connection() as conn:
            cur = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_cron'")
            return cur.fetchone() is not None
    except Exception:
        # If we can't probe, fall back to in-process. Don't crash startup over
        # a sweep-mode detection failure — the in-process path always works.
        logger.warning("pg_cron probe failed; falling back to in-process sweep", exc_info=True)
        return False


def _install_pg_cron_sweep(pool: ConnectionPool) -> None:
    """Install (or replace) a pg_cron job that sweeps expired replay rows."""
    with pool.connection() as conn:
        conn.execute(
            "SELECT cron.schedule(%s, '* * * * *', %s)",
            (
                PG_CRON_JOB_NAME,
                "DELETE FROM adcp_replay WHERE expires_at <= now()",
            ),
        )


async def _in_process_sweep_loop(store: PgReplayStore, interval: float) -> None:
    """Run ``store.sweep_expired()`` on a fixed interval until cancelled.

    The sweep is sync psycopg work — run on a thread so it doesn't block the
    event loop on every tick (noticeable on single-worker deployments where
    a stalled DB connection would freeze all in-flight requests).
    """
    while True:
        try:
            await asyncio.to_thread(store.sweep_expired)
        except Exception:
            logger.warning("adcp_replay in-process sweep failed", exc_info=True)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return


def bootstrap_replay_store(
    *,
    sweep_interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
) -> PgReplayStore:
    """Bootstrap the replay store + sweep mechanism. Call once at startup.

    Returns the store singleton. Idempotent across multiple calls (the
    ``create_schema()`` call is itself idempotent; the sweep task is only
    spawned the first time).
    """
    store = get_replay_store()
    pool = _get_pool()

    try:
        store.create_schema()
    except Exception:
        logger.error("Failed to bootstrap adcp_replay schema; continuing", exc_info=True)

    mode = os.getenv("REPLAY_SWEEP_MODE", "auto").lower()
    if mode == "off":
        logger.info("adcp_replay sweep disabled (REPLAY_SWEEP_MODE=off)")
        return store

    chosen = mode
    if mode == "auto":
        chosen = "pg_cron" if _has_pg_cron(pool) else "in_process"

    if chosen == "pg_cron":
        try:
            _install_pg_cron_sweep(pool)
            logger.info("adcp_replay sweep installed via pg_cron")
            return store
        except Exception:
            logger.warning(
                "pg_cron sweep install failed; falling back to in-process",
                exc_info=True,
            )
            chosen = "in_process"

    if chosen == "in_process":
        global _sweep_task
        if _sweep_task is None or _sweep_task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.info(
                    "adcp_replay in-process sweep skipped — no running event loop "
                    "at bootstrap (tests). Will start on first verifier call."
                )
                return store
            _sweep_task = loop.create_task(_in_process_sweep_loop(store, sweep_interval_seconds))
            logger.info(
                "adcp_replay sweep running in-process every %.0fs",
                sweep_interval_seconds,
            )

    return store


def reset_for_tests() -> None:
    """Tear down singletons — for test isolation only."""
    global _pool, _store, _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        _sweep_task.cancel()
    _sweep_task = None
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
    _pool = None
    _store = None
