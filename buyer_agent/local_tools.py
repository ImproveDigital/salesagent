"""Local tools: tools the harness runs itself, not the sales agent.

From the model's point of view these look exactly like the sales agent's MCP
tools: a name, a description and a JSON Schema for arguments. The difference
is who executes them. ``ask_user`` is the first one: it turns the human into a
tool the model can call when it lacks information.

Each local tool is a ``LocalTool`` with the same three fields an MCP tool
exposes plus an async ``run`` function, so the registry can treat both kinds
the same way. They are built per run, around the ``Human`` for that run, so
batch mode can answer from a script instead of the keyboard.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from buyer_agent import render
from buyer_agent.human import Human


@dataclass(frozen=True)
class LocalTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[dict[str, Any]], Awaitable[Any]]


def _ask_user(human: Human) -> Callable[[dict[str, Any]], Awaitable[Any]]:
    async def run(arguments: dict[str, Any]) -> dict[str, str]:
        question = str(arguments.get("question", "")).strip()
        render.agent_text(question)
        answer = await human.answer(question)
        if answer is None:
            # Batch mode with the script exhausted. The model must conclude with what it has.
            return {"error": "no operator available to answer; finish with the information you have"}
        return {"answer": answer}

    return run


def build_local_tools(human: Human) -> dict[str, LocalTool]:
    ask_user = LocalTool(
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
        run=_ask_user(human),
    )
    return {ask_user.name: ask_user}
