#!/usr/bin/env python3
"""Buyer-side agent harness for testing the sales agent over MCP.

Entry point. Settings are read from environment variables, which may live in
the project's gitignored ``.env`` file:
    SALES_AGENT_MCP_URL   e.g. http://localhost:8000/mcp/
    SALES_AGENT_TOKEN     principal token for the sales agent
    GEMINI_API_KEY        Gemini API key
    GEMINI_MODEL          optional, model id

Run as a module from the repo root, so that ``buyer_agent`` is importable:
    uv run python -m buyer_agent.main "Find display products for testbrand.com"
    uv run python -m buyer_agent.main "..." --tools get_products,create_media_buy
    uv run python -m buyer_agent.main "..." --show-history
"""

import argparse
import asyncio
import json
import os
from typing import Any

from dotenv import load_dotenv
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from google.genai import types

from buyer_agent import llm
from buyer_agent.local_tools import LOCAL_TOOLS
from buyer_agent.registry import ToolEntry, build_registry

load_dotenv()

SYSTEM_PROMPT = """You are a media buyer working with an advertising sales agent through tools.
Work towards the user's goal by calling tools. Never invent IDs, domains, budgets or dates:
if a value is missing, discover it with a read-only tool or ask the user with ask_user.
Before calling a tool that creates or changes something, summarise what you are about to do
and confirm with the user via ask_user.
When the goal is achieved, or cannot be, reply in plain text with a short summary."""

MAX_TURNS = 10


def connect() -> Client:
    """Build an MCP client for the sales agent from environment variables."""
    url = os.environ["SALES_AGENT_MCP_URL"]
    token = os.environ["SALES_AGENT_TOKEN"]
    transport = StreamableHttpTransport(url=url, headers={"x-adcp-auth": token})
    return Client(transport=transport)


async def confirm(entry: ToolEntry, arguments: dict[str, Any]) -> bool:
    """The gate. Show the human exactly what would run and wait for a yes.

    This is enforced by the harness regardless of what the model said or was
    told. The prompt may ask the model to confirm via ask_user as well, but
    the prompt is advice; this is the rule.
    """
    print(f"\n[confirm] {entry.name} changes state on the sales agent. Arguments:")
    print(json.dumps(arguments, indent=2))
    reply = await asyncio.to_thread(input, "[confirm] run it? [y/N] > ")
    return reply.strip().lower() in {"y", "yes"}


async def execute(registry: dict[str, ToolEntry], name: str, arguments: dict[str, Any]) -> Any:
    """Look the tool up, pass it through the gate if flagged, run it.

    Every outcome comes back as data for the model: an unknown tool, a
    declined confirmation and a tool error are all results, not crashes.
    """
    entry = registry.get(name)
    if entry is None:
        return {"error": f"unknown tool '{name}'"}
    if entry.requires_confirmation and not await confirm(entry, arguments):
        return {"error": "declined by the operator; do not retry without asking them"}
    try:
        return await entry.run(arguments)
    except ToolError as exc:
        return {"error": str(exc)}


def print_history(history: list[types.Content]) -> None:
    """Show what the model will see: every turn so far, one line per part."""
    print(f"\n[history: {len(history)} turn(s)]")
    for i, content in enumerate(history):
        for part in content.parts or []:
            if part.text:
                print(f"  {i}. {content.role:5} text: {part.text.strip()[:200]}")
            elif part.function_call:
                args = json.dumps(dict(part.function_call.args or {}))
                print(f"  {i}. {content.role:5} call: {part.function_call.name} {args[:200]}")
            elif part.function_response:
                body = json.dumps(part.function_response.response)
                print(f"  {i}. {content.role:5} result: {part.function_response.name} {body[:200]}")


async def run_agent(
    registry: dict[str, ToolEntry],
    goal: str,
    show_history: bool = False,
) -> str:
    """The agent loop: model proposes, harness executes, result goes back. Repeat.

    Ends when the model replies with text instead of tool calls, or when the
    turn limit is hit. The limit lives here, not in the prompt, so a confused
    model cannot run forever.

    Tool schemas go to Gemini exactly as the sales agent serves them. If Gemini
    rejects the request, the error propagates: that is a test result.
    """
    client = llm.make_client()
    gemini_tools = [entry.to_gemini() for entry in registry.values()]
    history: list[types.Content] = [llm.user_turn(goal)]

    for turn in range(1, MAX_TURNS + 1):
        if show_history:
            print_history(history)
        response = await llm.step(client, history, gemini_tools, SYSTEM_PROMPT)
        model_turn = response.candidates[0].content
        history.append(model_turn)

        calls = list(response.function_calls or [])
        if not calls:
            return response.text or "(model returned no text)"

        print(f"\n--- turn {turn}: {len(calls)} tool call(s) ---")
        result_parts: list[types.Part] = []
        for call in calls:
            arguments = dict(call.args or {})
            print(f"  -> {call.name} {json.dumps(arguments)}")
            result = await execute(registry, call.name, arguments)
            print(f"  <- {json.dumps(result)[:500]}")
            result_parts.append(llm.tool_result_part(call.name, result))

        history.append(llm.tool_results_turn(result_parts))

    return f"(stopped after {MAX_TURNS} turns without a final answer)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the buyer agent against the sales agent.")
    parser.add_argument("goal", help="What the buyer wants, in plain language.")
    parser.add_argument(
        "--tools",
        help="Comma-separated sales agent tools to expose. Default: all. Local tools are always included.",
    )
    parser.add_argument("--show-history", action="store_true", help="Print the history before each model turn.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    allow = set(args.tools.split(",")) if args.tools else None

    async with connect() as mcp:
        registry = await build_registry(mcp, LOCAL_TOOLS, allow)
        gated = sorted(e.name for e in registry.values() if e.requires_confirmation)
        print(f"Tools exposed: {len(registry)} ({', '.join(sorted(registry))})")
        print(f"Gated (need confirmation): {', '.join(gated) or 'none'}")

        print(f"\nGoal: {args.goal}")
        answer = await run_agent(registry, args.goal, show_history=args.show_history)
        print(f"\n=== agent finished ===\n{answer}")


if __name__ == "__main__":
    asyncio.run(main())
