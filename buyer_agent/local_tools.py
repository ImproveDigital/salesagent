"""Local tools: tools the harness runs itself, not the sales agent.

From the model's point of view these look exactly like the sales agent's MCP
tools: a name, a description and a JSON Schema for arguments. The difference
is who executes them. ``ask_user`` is the first one: it turns the human into a
tool the model can call when it lacks information.

Each local tool is a ``LocalTool`` with the same three fields an MCP tool
exposes plus an async ``run`` function, so the registry can treat both kinds
the same way.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LocalTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[dict[str, Any]], Awaitable[Any]]


async def _ask_user(arguments: dict[str, Any]) -> dict[str, str]:
    """Print the model's question, block for a keyboard answer, return it.

    ``input()`` blocks the whole thread, so it runs in a worker thread via
    ``asyncio.to_thread``. That keeps the event loop free for anything else
    the harness may be doing while the human types.
    """
    question = str(arguments.get("question", "")).strip()
    print(f"\n[agent] {question}")
    answer = await asyncio.to_thread(input, "[user] > ")
    answer = answer.strip()
    return {"answer": answer}


ASK_USER = LocalTool(
    name="ask_user",
    description=(
        "Ask the human operator a question and wait for their answer. Use this "
        "when a required value is missing and cannot be discovered with another "
        "tool, or when the human must choose between options. Ask one clear "
        "question at a time. Never invent IDs, budgets or dates instead of asking."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to show the human, including any options to pick from.",
            }
        },
        "required": ["question"],
    },
    run=_ask_user,
)

LOCAL_TOOLS: dict[str, LocalTool] = {ASK_USER.name: ASK_USER}
