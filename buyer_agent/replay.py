"""Replay recorded tool calls against the sales agent, in parallel, without a model.

    uv run python -m buyer_agent.replay runs/<log>.jsonl                 # once, one client
    uv run python -m buyer_agent.replay runs/<log>.jsonl --clients 10 --rounds 3
    uv run python -m buyer_agent.replay runs/*.jsonl --read-only --dry-run

A run log records what the buyer sent and in what order. This takes those
calls (sales agent tools only, never ask_user) and sends them again from
``--clients`` independent MCP connections, each repeating the sequence
``--rounds`` times. Every call is timed and its error, if any, kept. Gemini is
not involved, so the numbers measure the sales agent and nothing else.

Writes are replayed too unless ``--read-only``: that is how create_media_buy
gets load tested, but it creates real objects and identical repeats may be
rejected as duplicates. Both are findings; use ``local`` as the target.

Output: a per-tool latency table on the console, every call as JSONL and a
summary JSON under buyer_agent/runs/replay/<timestamp>/.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from rich import box
from rich.table import Table

from buyer_agent.grader import read_log
from buyer_agent.registry import WRITE_TOOLS
from buyer_agent.render import console
from buyer_agent.runlog import RUNS_DIR, RunLog, error_info

Call = dict[str, Any]  # {"name": ..., "arguments": {...}}


def recorded_calls(logs: list[Path], read_only: bool) -> list[Call]:
    calls = []
    for log in logs:
        for event in read_log(log):
            if event["event"] != "tool_call" or event.get("source") != "mcp":
                continue
            if read_only and event["name"] in WRITE_TOOLS:
                continue
            calls.append({"name": event["name"], "arguments": event["arguments"]})
    return calls


def connect() -> Client:
    transport = StreamableHttpTransport(
        url=os.environ["SALES_AGENT_MCP_URL"], headers={"x-adcp-auth": os.environ["SALES_AGENT_TOKEN"]}
    )
    return Client(transport=transport)


async def one_client(client_id: int, calls: list[Call], rounds: int, log: RunLog) -> list[dict[str, Any]]:
    """Open one connection and send the whole sequence ``rounds`` times, in order."""
    samples = []
    async with connect() as mcp:
        for round_no in range(1, rounds + 1):
            for index, call in enumerate(calls):
                started = time.perf_counter()
                error = None
                try:
                    await mcp.call_tool(call["name"], call["arguments"])
                except Exception as exc:
                    error = error_info(exc)
                sample = {
                    "client": client_id,
                    "round": round_no,
                    "index": index,
                    "name": call["name"],
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "error": error,
                }
                log.write("replay_call", **sample)
                samples.append(sample)
    return samples


async def run(calls: list[Call], clients: int, rounds: int, log: RunLog) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    results = await asyncio.gather(
        *(one_client(i, calls, rounds, log) for i in range(1, clients + 1)), return_exceptions=True
    )
    samples: list[dict[str, Any]] = []
    for client_id, result in enumerate(results, start=1):
        if isinstance(result, BaseException):
            # The connection itself failed: nothing was measured for this client, but the failure counts.
            samples.append(
                {
                    "client": client_id,
                    "round": 0,
                    "index": -1,
                    "name": "(connect)",
                    "duration_ms": 0,
                    "error": error_info(result),
                }
            )
            log.write("replay_call", **samples[-1])
        else:
            samples.extend(result)
    return samples, time.perf_counter() - started


def percentile(values: list[int], pct: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))]


def summarise(samples: list[dict[str, Any]], wall_seconds: float) -> dict[str, Any]:
    by_tool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in samples:
        by_tool[s["name"]].append(s)
    tools = {}
    for name, rows in by_tool.items():
        durations = [r["duration_ms"] for r in rows]
        errors: dict[str, int] = defaultdict(int)
        for r in rows:
            if r["error"]:
                errors[f"{r['error']['type']}: {r['error']['message'][:120]}"] += 1
        tools[name] = {
            "calls": len(rows),
            "errors": sum(errors.values()),
            "p50_ms": percentile(durations, 50),
            "p95_ms": percentile(durations, 95),
            "max_ms": max(durations),
            "error_messages": dict(errors),
        }
    total = len(samples)
    return {
        "calls": total,
        "errors": sum(1 for s in samples if s["error"]),
        "wall_seconds": round(wall_seconds, 2),
        "calls_per_second": round(total / wall_seconds, 1) if wall_seconds else None,
        "tools": tools,
    }


def report(summary: dict[str, Any]) -> None:
    table = Table(box=box.ROUNDED, header_style="bold")
    table.add_column("tool")
    for column in ("calls", "errors", "p50 ms", "p95 ms", "max ms"):
        table.add_column(column, justify="right")
    for name, t in summary["tools"].items():
        style = "red" if t["errors"] else ""
        table.add_row(
            name, str(t["calls"]), str(t["errors"]), str(t["p50_ms"]), str(t["p95_ms"]), str(t["max_ms"]), style=style
        )
    console.print(table)
    for name, t in summary["tools"].items():
        for message, count in t["error_messages"].items():
            console.print(f"  {name} x{count}: {message}", style="red", markup=False, highlight=False)
    console.print(
        f"[harness] {summary['calls']} call(s), {summary['errors']} error(s), "
        f"{summary['wall_seconds']}s wall, {summary['calls_per_second']} calls/s",
        markup=False,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay recorded tool calls against the sales agent in parallel.")
    parser.add_argument("logs", nargs="+", type=Path, help="Run log(s) to replay, in order.")
    parser.add_argument("--clients", type=int, default=1, help="Parallel connections. Default 1.")
    parser.add_argument("--rounds", type=int, default=1, help="Times each client repeats the sequence. Default 1.")
    parser.add_argument("--read-only", action="store_true", help="Skip tools that create or change state.")
    parser.add_argument("--dry-run", action="store_true", help="List the calls that would be sent and exit.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    calls = recorded_calls(args.logs, args.read_only)
    if not calls:
        sys.exit("[harness] no sales agent calls found in the given log(s)")
    writes = sorted({c["name"] for c in calls if c["name"] in WRITE_TOOLS})
    console.print(f"[harness] {len(calls)} call(s) per round: {', '.join(c['name'] for c in calls)}", markup=False)
    if writes:
        console.print(f"[harness] includes writes ({', '.join(writes)}): real objects will be created", style="yellow")
    if args.dry_run:
        for c in calls:
            print(json.dumps(c))
        return
    out_dir = RUNS_DIR / "replay" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log = RunLog(out_dir / "calls.jsonl")
    console.print(f"[harness] {args.clients} client(s) x {args.rounds} round(s) -> {out_dir}", markup=False)
    try:
        samples, wall = asyncio.run(run(calls, args.clients, args.rounds, log))
    finally:
        log.close()
    summary = summarise(samples, wall)
    summary["config"] = {
        "logs": [str(p) for p in args.logs],
        "clients": args.clients,
        "rounds": args.rounds,
        "read_only": args.read_only,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    report(summary)
    sys.exit(1 if summary["errors"] else 0)


if __name__ == "__main__":
    main()
