"""The run log: one JSONL file per run, one event per line, nothing truncated.

The console shows a human what is happening, shortened. This file is the
record a grader, a replay or a person can read afterwards. Every event has
``ts`` (UTC ISO) and ``event``; the rest depends on the event:

  run_start   goal, target, model, batch, tools
  model_turn  turn, text, calls [{name, arguments}], usage {input, output, thoughts, total}
  tool_call   turn, name, source, arguments, result, gate (approved|declined|None),
              error {type, message} or None, duration_ms
  user_turn   text (a follow-up goal)
  model_error turn, error {type, message, code}
  run_end     outcome, usage (session totals)

Each line is flushed as it is written, so a crash keeps everything up to it.
Default location: buyer_agent/runs/<UTC timestamp>_<goal slug>.jsonl
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

RUNS_DIR = Path(__file__).parent / "runs"


def default_path(goal: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:40] or "run"
    return RUNS_DIR / f"{stamp}_{slug}.jsonl"


class RunLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file: IO[str] = path.open("w", encoding="utf-8")

    def write(self, event: str, **fields: Any) -> None:
        record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "event": event, **fields}
        # default=str keeps the log alive when a result holds something json cannot encode (dates, Decimals).
        self._file.write(json.dumps(record, default=str) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def error_info(exc: BaseException) -> dict[str, Any]:
    """The part of an exception worth keeping: type, message, and an HTTP code if it has one."""
    info: dict[str, Any] = {"type": f"{type(exc).__module__}.{type(exc).__name__}", "message": str(exc)}
    code = getattr(exc, "code", None)
    if code is not None:
        info["code"] = code
    return info
