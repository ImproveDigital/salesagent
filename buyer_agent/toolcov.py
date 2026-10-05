"""Tool-level coverage: which sales agent tools, and which of their arguments, the runs have exercised.

    uv run python -m buyer_agent.toolcov                 # every log under buyer_agent/runs/
    uv run python -m buyer_agent.toolcov runs/evals/<ts>  # one directory or file

Reads run logs, unions the tools each run_start listed, and counts per tool
the calls, the errors and the distinct top-level argument keys sent. Tools
never called are listed at the end: that is the gap list for new scenarios.
Replay logs are included, since they hit the sales agent too.
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from rich import box
from rich.table import Table

from buyer_agent.grader import read_log
from buyer_agent.render import console
from buyer_agent.runlog import RUNS_DIR


def collect(paths: list[Path]) -> tuple[set[str], dict[str, dict[str, Any]]]:
    files = [f for p in paths for f in (sorted(p.rglob("*.jsonl")) if p.is_dir() else [p])]
    listed: set[str] = set()
    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "errors": 0, "arg_keys": set(), "runs": set()})
    for file in files:
        try:
            events = read_log(file)
        except ValueError:
            print(f"[harness] skipping {file}: not valid JSONL", file=sys.stderr)
            continue
        for e in events:
            if e["event"] == "run_start":
                listed.update(t for t in e["tools"] if t != "ask_user")
            elif e["event"] == "tool_call" and e.get("source") == "mcp" or e["event"] == "replay_call":
                s = stats[e["name"]]
                s["calls"] += 1
                s["errors"] += bool(e.get("error"))
                s["arg_keys"].update((e.get("arguments") or {}).keys())
                s["runs"].add(file.stem)
    return listed, stats


def report(listed: set[str], stats: dict[str, dict[str, Any]]) -> None:
    table = Table(box=box.ROUNDED, header_style="bold")
    table.add_column("tool")
    table.add_column("calls", justify="right")
    table.add_column("errors", justify="right")
    table.add_column("runs", justify="right")
    table.add_column("argument keys seen")
    for name in sorted(listed | set(stats)):
        s = stats.get(name)
        if s is None:
            table.add_row(name, "0", "-", "0", "-", style="red")
        else:
            table.add_row(
                name, str(s["calls"]), str(s["errors"]), str(len(s["runs"])), ", ".join(sorted(s["arg_keys"]))
            )
    console.print(table)
    never = sorted(listed - set(stats))
    covered = len(listed & set(stats))
    console.print(f"[harness] {covered}/{len(listed)} listed tool(s) called at least once", markup=False)
    if never:
        console.print(f"[harness] never called: {', '.join(never)}", style="red", markup=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Tool-level coverage from buyer agent run logs.")
    parser.add_argument("paths", nargs="*", type=Path, default=[RUNS_DIR], help="Log files or directories.")
    args = parser.parse_args()
    listed, stats = collect(args.paths)
    if not listed and not stats:
        sys.exit("[harness] no run logs found")
    report(listed, stats)


if __name__ == "__main__":
    main()
