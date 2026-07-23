#!/usr/bin/env python3
"""Run the AdCP Sales Agent with HTTP transport.

Delegates to ``core.main.main()`` — one Starlette binary serves MCP at /mcp,
A2A at /, and Flask admin via WSGI middleware.
"""

import os
import sys


def main():
    """Run the server with configurable port."""
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

    # ADCP_SALES_PORT is this launcher's operator-facing knob (compose sets it
    # to 8080 and healthchecks that port). Assign — not setdefault — so the
    # image-baked ENV ADCP_PORT=8000 (used by the run_all_services entrypoint)
    # can't silently win and leave nginx proxying to a port nobody listens on.
    os.environ["ADCP_PORT"] = str(port)
    from core.main import main as _core_main

    try:
        _core_main()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
