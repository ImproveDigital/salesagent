"""Grade one run log against a scenario's expectations. Pure: events in, failures out.

A scenario's ``expect`` block may hold any of:

  outcome: completed            the run ended with a final answer (not stopped, crashed or errored)
  calls:                        per tool, how often it may be called and with what
    - tool: get_products
      min: 1                    default 0
      max: 3                    default unlimited
      args_contain:             every listed key must appear in the arguments with this value
        brand: testbrand.com
  tool_errors: none             no tool call may fail (sales agent error, transport, unknown tool)
  tool_errors:                  or: these failures are required, any others are failures
    - tool: create_media_buy
      message_contains: budget
  final_text_contains:          each substring must appear in the final answer, case-insensitive
    - display

Every check that does not hold becomes one human-readable failure string.
"""

import json
from pathlib import Path
from typing import Any

Event = dict[str, Any]


def read_log(path: Path) -> list[Event]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def grade(expect: dict[str, Any], events: list[Event]) -> list[str]:
    calls = [e for e in events if e["event"] == "tool_call"]
    failures: list[str] = []
    failures += _check_outcome(expect.get("outcome"), events)
    failures += _check_calls(expect.get("calls") or [], calls)
    failures += _check_tool_errors(expect.get("tool_errors"), calls)
    failures += _check_final_text(expect.get("final_text_contains") or [], events)
    return failures


def _check_outcome(wanted: str | None, events: list[Event]) -> list[str]:
    if wanted is None:
        return []
    end = next((e for e in events if e["event"] == "run_end"), None)
    outcome = end["outcome"] if end else "(no run_end event: the run crashed)"
    if wanted == "completed" and outcome != "(completed)":
        return [f"outcome: wanted a completed run, got {outcome}"]
    return []


def _check_calls(rules: list[dict[str, Any]], calls: list[Event]) -> list[str]:
    failures = []
    for rule in rules:
        tool = rule["tool"]
        matching = [c for c in calls if c["name"] == tool]
        lo, hi = rule.get("min", 0), rule.get("max")
        if len(matching) < lo:
            failures.append(f"{tool}: called {len(matching)} time(s), wanted at least {lo}")
        if hi is not None and len(matching) > hi:
            failures.append(f"{tool}: called {len(matching)} time(s), wanted at most {hi}")
        wanted_args = rule.get("args_contain") or {}
        if wanted_args and matching and not any(_contains(c["arguments"], wanted_args) for c in matching):
            failures.append(f"{tool}: no call had arguments containing {json.dumps(wanted_args)}")
    return failures


def _contains(actual: Any, wanted: Any) -> bool:
    """``wanted`` is a subset of ``actual``: dicts by key, lists element-wise, scalars by equality."""
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(k in actual and _contains(actual[k], v) for k, v in wanted.items())
    if isinstance(wanted, list):
        return isinstance(actual, list) and all(any(_contains(a, w) for a in actual) for w in wanted)
    return bool(actual == wanted)


def _check_tool_errors(rule: Any, calls: list[Event]) -> list[str]:
    if rule is None:
        return []
    failed = [c for c in calls if c.get("error")]
    if rule == "none":
        return [f"{c['name']}: failed with {c['error']['message']}" for c in failed]
    failures = []
    for wanted in rule:
        tool, fragment = wanted["tool"], wanted.get("message_contains", "")
        if not any(c["name"] == tool and fragment.lower() in c["error"]["message"].lower() for c in failed):
            failures.append(f"{tool}: expected an error containing {fragment!r}, none seen")
    expected_tools = {w["tool"] for w in rule}
    failures += [
        f"{c['name']}: unexpected error {c['error']['message']}" for c in failed if c["name"] not in expected_tools
    ]
    return failures


def _check_final_text(fragments: list[str], events: list[Event]) -> list[str]:
    if not fragments:
        return []
    texts = [e["text"] for e in events if e["event"] == "model_turn" and e.get("text")]
    final = (texts[-1] if texts else "").lower()
    return [f"final answer does not mention {f!r}" for f in fragments if f.lower() not in final]
