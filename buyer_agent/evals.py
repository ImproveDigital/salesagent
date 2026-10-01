"""Run scenarios against the sales agent and grade the logs. The eval runner.

    uv run python -m buyer_agent.evals                       # every scenario in buyer_agent/scenarios/
    uv run python -m buyer_agent.evals scenarios/foo.yaml    # one file or directory
    uv run python -m buyer_agent.evals --runs 3              # repeat each scenario, report pass rate

Each run is a separate ``buyer_agent.main --batch`` process with its own log,
so a crash in one run cannot take the others down and every log stays
inspectable. Results go to buyer_agent/runs/evals/<timestamp>/ as logs plus
a results.json. Exit code 1 when any run fails.

A scenario file:

    name: find-display-products
    goal: Find display products for testbrand.com
    answers: []            # scripted ask_user answers, optional
    facts: {budget: 5000}  # turns on the simulator for ask_user, optional
    persona: "..."         # how the simulated user talks, optional
    tools: [get_products]  # tools to expose, optional (default: all)
    expect: {...}          # see grader.py
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped, unused-ignore]
from rich import box
from rich.table import Table

from buyer_agent import grader
from buyer_agent.render import console
from buyer_agent.runlog import RUNS_DIR

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def load_scenarios(target: Path) -> list[dict[str, Any]]:
    files = sorted(target.glob("*.yaml")) if target.is_dir() else [target]
    scenarios = []
    for file in files:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
        for key in ("name", "goal", "expect"):
            if key not in data:
                sys.exit(f"{file}: missing '{key}'")
        scenarios.append(data)
    return scenarios


async def run_once(scenario: dict[str, Any], log_path: Path) -> int:
    """Launch the harness in batch mode for one scenario. Returns the exit code."""
    cmd = [sys.executable, "-m", "buyer_agent.main", scenario["goal"], "--batch", "--log", str(log_path)]
    for answer in scenario.get("answers") or []:
        cmd += ["--answer", str(answer)]
    if scenario.get("tools"):
        cmd += ["--tools", ",".join(scenario["tools"])]
    if "facts" in scenario:
        cmd += ["--simulate"]
        for key, value in (scenario["facts"] or {}).items():
            cmd += ["--fact", f"{key}={value}"]
        if scenario.get("persona"):
            cmd += ["--persona", scenario["persona"]]
    with log_path.with_suffix(".out").open("w", encoding="utf-8") as console_out:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=console_out, stderr=asyncio.subprocess.STDOUT)
        return await proc.wait()


async def run_all(scenarios: list[dict[str, Any]], runs: int, out_dir: Path) -> list[dict[str, Any]]:
    results = []
    for scenario in scenarios:
        for i in range(1, runs + 1):
            log_path = out_dir / f"{scenario['name']}_{i}.jsonl"
            console.print(f"[harness] {scenario['name']} run {i}/{runs} ...", style="dim", markup=False)
            code = await run_once(scenario, log_path)
            events = grader.read_log(log_path) if log_path.exists() else []
            failures = grader.grade(scenario["expect"], events)
            if code != 0:
                failures.append(f"harness exited with code {code}, see {log_path.with_suffix('.out').name}")
            results.append({"scenario": scenario["name"], "run": i, "passed": not failures, "failures": failures})
    return results


def report(results: list[dict[str, Any]]) -> None:
    table = Table(box=box.ROUNDED, header_style="bold")
    for column in ("scenario", "passed", "failures"):
        table.add_column(column)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_name.setdefault(r["scenario"], []).append(r)
    for name, rs in by_name.items():
        passed = sum(r["passed"] for r in rs)
        failures = [f"run {r['run']}: {f}" for r in rs for f in r["failures"]]
        table.add_row(name, f"{passed}/{len(rs)}", "\n".join(failures) or "-")
    console.print(table)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run buyer agent scenarios and grade them.")
    parser.add_argument("target", nargs="?", type=Path, default=SCENARIOS_DIR, help="Scenario file or directory.")
    parser.add_argument("--runs", type=int, default=1, help="Times to run each scenario.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    scenarios = load_scenarios(args.target)
    out_dir = RUNS_DIR / "evals" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True)
    results = asyncio.run(run_all(scenarios, args.runs, out_dir))
    (out_dir / "results.json").write_text(json.dumps(results, indent=2))
    report(results)
    total, passed = len(results), sum(r["passed"] for r in results)
    console.print(f"[harness] {passed}/{total} run(s) passed. Logs: {out_dir}", markup=False)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
