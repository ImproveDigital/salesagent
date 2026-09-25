"""Token accounting for the agent loop.

Gemini reports usage per call. This keeps three counters: the last call, the
current goal (reset at each follow-up) and the whole session, and renders them
in the harness without spending model tokens.
"""

from dataclasses import dataclass

from google.genai import types
from rich import box
from rich.table import Table

from buyer_agent.render import console


@dataclass
class Usage:
    input: int = 0
    cached: int = 0
    output: int = 0
    thoughts: int = 0
    total: int = 0
    calls: int = 0

    def add(self, meta: types.GenerateContentResponseUsageMetadata | None) -> None:
        self.calls += 1
        if meta is None:
            return
        self.input += meta.prompt_token_count or 0
        self.cached += meta.cached_content_token_count or 0
        self.output += meta.candidates_token_count or 0
        self.thoughts += meta.thoughts_token_count or 0
        self.total += meta.total_token_count or 0


class Tracker:
    def __init__(self) -> None:
        self.last = Usage()
        self.goal = Usage()
        self.session = Usage()

    def record(self, meta: types.GenerateContentResponseUsageMetadata | None) -> None:
        self.last = Usage()
        self.last.add(meta)
        self.goal.add(meta)
        self.session.add(meta)

    def new_goal(self) -> None:
        self.goal = Usage()

    def print_call_line(self) -> None:
        last, session = self.last, self.session
        parts = [f"in {last.input:,}"]
        if last.cached:
            parts[0] += f" (cached {last.cached:,})"
        parts.append(f"out {last.output:,}")
        if last.thoughts:
            parts.append(f"thoughts {last.thoughts:,}")
        parts.append(f"total {last.total:,}")
        parts.append(f"session {session.total:,}")
        console.print(f"  tokens: {' | '.join(parts)}", style="dim", markup=False, highlight=False)

    def print_goal_summary(self) -> None:
        table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
        table.add_column("")
        for name in ("in", "cached", "out", "thoughts", "total", "calls"):
            table.add_column(name, justify="right")
        for label, u in (("this goal", self.goal), ("session", self.session)):
            table.add_row(
                label, f"{u.input:,}", f"{u.cached:,}", f"{u.output:,}", f"{u.thoughts:,}", f"{u.total:,}", str(u.calls)
            )
        console.print(table)

    def print_session_line(self, model: str) -> None:
        u = self.session
        console.print(
            f"[harness] session tokens: in {u.input:,} | out {u.output:,} | thoughts {u.thoughts:,} "
            f"| total {u.total:,} over {u.calls} call(s) to {model}",
            markup=False,
            highlight=False,
        )
