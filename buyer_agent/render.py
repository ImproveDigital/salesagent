"""Terminal rendering for model text.

The model writes plain Markdown. Lines of the form ``- **Key**: value`` are
shown as a two-column table; everything else goes through rich's Markdown
renderer. Formatting is done here, not by the model, so it costs no tokens.
"""

import re

from rich import box
from rich.console import Console, RenderableType
from rich.markdown import Markdown
from rich.table import Table

_KV_LINE = re.compile(r"^(\s*)[-*]\s+\*\*(.+?)\*\*\s*:?\s*(.*)$")

console = Console()


def _table(rows: list[tuple[int, str, str]]) -> Table:
    table = Table(show_header=False, box=box.ROUNDED, show_lines=True)
    table.add_column(style="bold")
    table.add_column()
    for indent, key, value in rows:
        table.add_row("  " * indent + key, value.replace("`", ""))
    return table


def _blocks(text: str) -> list[RenderableType]:
    blocks: list[RenderableType] = []
    rows: list[tuple[int, str, str]] = []
    prose: list[str] = []

    def flush_rows() -> None:
        if rows:
            blocks.append(_table(list(rows)))
            rows.clear()

    def flush_prose() -> None:
        if any(line.strip() for line in prose):
            blocks.append(Markdown("\n".join(prose)))
        prose.clear()

    for line in text.splitlines():
        match = _KV_LINE.match(line)
        if match:
            flush_prose()
            indent = len(match.group(1)) // 2
            rows.append((indent, match.group(2).strip().rstrip(":"), match.group(3).strip()))
        else:
            flush_rows()
            prose.append(line)
    flush_prose()
    flush_rows()
    return blocks


def agent_text(text: str) -> None:
    """Print model text under an ``[agent]`` label, tables for key-value bullets."""
    blocks = _blocks(text)
    if len(blocks) == 1 and isinstance(blocks[0], Markdown) and "\n" not in text.strip():
        console.print(f"\n[agent] {text.strip()}", markup=False)
        return
    console.print("\n[agent]", markup=False)
    for block in blocks:
        console.print(block)
