"""Local webhook receiver for manually testing AdCP push notifications.

Stdlib only. Prints every incoming POST (headers + pretty JSON body) and,
when ``--secret`` is given, verifies the legacy AdCP HMAC signature
(``X-ADCP-Signature`` over ``"{X-ADCP-Timestamp}.{raw_body}"``).

Usage:
    python scripts/dev/webhook_receiver.py --port 9999
    python scripts/dev/webhook_receiver.py --port 9999 --secret <same-secret-as-push_notification_config>

Then register ``http://127.0.0.1:9999/webhook`` as the push_notification_config
URL. Use 127.0.0.1, not ``localhost`` — the sender rewrites ``localhost`` to
``host.docker.internal``, which does not resolve on a bare macOS process.

Requires the sales agent to run with ``ADCP_AUTH_TEST_MODE=true`` or
``WEBHOOK_ALLOW_PRIVATE_IPS=true`` so loopback URLs pass the SSRF check.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET: str | None = None
COUNT = 0


def _verify(headers: dict[str, str], body: bytes) -> str:
    if SECRET is None:
        return "not checked (no --secret)"
    sig = headers.get("x-adcp-signature")
    ts = headers.get("x-adcp-timestamp")
    if not sig or not ts:
        return "MISSING X-ADCP-Signature / X-ADCP-Timestamp headers"
    expected = hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    provided = sig.split("=", 1)[1] if sig.startswith("sha256=") else sig
    if not hmac.compare_digest(expected, provided):
        return "INVALID signature"
    try:
        age = abs(time.time() - float(ts))
        if age > 300:
            return f"valid signature but STALE timestamp ({age:.0f}s old)"
    except ValueError:
        pass
    return "VALID"


class Handler(BaseHTTPRequestHandler):
    server_version = "adcp-webhook-receiver/0.1"

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        global COUNT
        COUNT += 1
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        headers = {k.lower(): v for k, v in self.headers.items()}

        print(f"\n{'=' * 78}")
        print(f"#{COUNT}  {datetime.now(UTC).isoformat(timespec='seconds')}  POST {self.path}")
        print("-- headers")
        for k, v in self.headers.items():
            print(f"  {k}: {v}")
        print(f"-- signature: {_verify(headers, body)}")
        print("-- body")
        try:
            parsed = json.loads(body)
            print(json.dumps(parsed, indent=2))
            if isinstance(parsed, dict):
                summary = {k: parsed.get(k) for k in ("task_type", "status", "task_id", "operation_id") if k in parsed}
                print(f"-- summary: {summary}")
        except ValueError:
            print(body.decode(errors="replace"))
        sys.stdout.flush()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"webhook receiver alive, {COUNT} webhook(s) received\n".encode())

    def log_message(self, fmt: str, *args: object) -> None:
        return  # suppress default access log; we print our own


def main() -> None:
    global SECRET
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--secret", help="HMAC secret matching push_notification_config.authentication.credentials")
    args = ap.parse_args()
    SECRET = args.secret

    server = HTTPServer((args.host, args.port), Handler)
    print(f"Listening on http://{args.host}:{args.port}/webhook  (Ctrl+C to stop)")
    print(f"HMAC verification: {'on' if SECRET else 'off'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\nStopped after {COUNT} webhook(s).")


if __name__ == "__main__":
    main()

