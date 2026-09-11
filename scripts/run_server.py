#!/usr/bin/env python3
"""Run the AdCP Sales Agent with HTTP transport.

Delegates to ``core.main.main()`` — one Starlette binary serves MCP at /mcp,
A2A at /, and Flask admin via WSGI middleware.
"""

import faulthandler
import os
import sys
import threading
import time


def _rss_mb() -> float:
    """Current resident set size in MB (Linux: /proc; elsewhere: peak RSS)."""
    try:
        with open("/proc/self/statm") as f:
            resident_pages = int(f.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def start_memory_watchdog() -> None:
    """Log RSS milestones and dump every thread's stack when memory runs away.

    The Fargate task gets OOM-killed (SIGKILL) within a second or two of an
    admin save while Container Insights never shows more than ~70% memory —
    the burst is faster than the 1-minute metric. SIGKILL cannot be caught,
    so ``faulthandler.enable()`` never fires for it. This thread polls RSS
    every 200 ms, logs each ±200 MB move, and when RSS crosses
    ``ADCP_MEM_DUMP_MB`` (default 2200) — and again every 500 MB above it —
    dumps all thread stacks to stderr so the log shows exactly which code
    path is allocating at the moment the process runs away.
    Set ``ADCP_MEM_DUMP_MB=0`` to disable.
    """
    threshold = int(os.environ.get("ADCP_MEM_DUMP_MB", "2200"))
    if threshold <= 0:
        return

    def run() -> None:
        next_dump = float(threshold)
        last_logged = 0.0
        while True:
            time.sleep(0.2)
            mb = _rss_mb()
            if abs(mb - last_logged) >= 200:
                print(f"[memwatch] rss={mb:.0f} MB", file=sys.stderr, flush=True)
                last_logged = mb
            if mb >= next_dump:
                print(
                    f"[memwatch] rss={mb:.0f} MB crossed {next_dump:.0f} MB — dumping all thread stacks",
                    file=sys.stderr,
                    flush=True,
                )
                faulthandler.dump_traceback(all_threads=True)
                next_dump += 500

    threading.Thread(target=run, name="memwatch", daemon=True).start()


def main():
    """Run the server with configurable port."""
    # Dump Python tracebacks of every thread to stderr on a hard crash
    # (segfault, abort, fatal signal) — otherwise the process just vanishes
    # and the proxy reports 502 with nothing in the application log.
    faulthandler.enable()
    start_memory_watchdog()
    try:
        sys.path.insert(0, ".")
        from src.core.startup import initialize_application

        print("Initializing AdCP Sales Agent...")
        initialize_application()
        print("Application initialization completed")

    except SystemExit:
        print("Application initialization failed - check logs")
        sys.exit(1)
    except Exception as e:
        print(f"Startup error: {e}")
        sys.exit(1)

    port = int(os.environ.get("ADCP_SALES_PORT", "8080"))
    host = os.environ.get("ADCP_SALES_HOST", "0.0.0.0")
    if os.environ.get("FLY_APP_NAME") or os.environ.get("PRODUCTION"):
        host = "0.0.0.0"

    print(f"Starting AdCP Sales Agent on {host}:{port}")
    print(f"Server endpoint: http://{host}:{port}/")

    os.environ.setdefault("ADCP_PORT", str(port))
    from core.main import main as _core_main

    try:
        _core_main()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
