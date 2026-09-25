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
import itertools
import json
import os
import shutil
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from google import genai
from google.genai import errors, types

from buyer_agent import llm, render, usage
from buyer_agent.local_tools import LOCAL_TOOLS
from buyer_agent.registry import ToolEntry, build_registry

load_dotenv()

SYSTEM_PROMPT_TEMPLATE = """You are a media buyer working with an advertising sales agent through tools.
Today's date is {today} (UTC). Use it to resolve relative dates such as "tomorrow".

Scope. You only handle advertising media buying through this sales agent: discovering
accounts and products, creative formats, creating and updating media buys, creatives,
delivery and reporting, and questions about what the sales agent offers or about this
conversation. If the user asks for anything outside that scope, decline in one sentence,
state what you can help with, and do not answer the out-of-scope request. Do not call
tools to answer an out-of-scope request.

Work towards the user's goal by calling tools. Never invent IDs, domains, budgets or dates:
if a value is missing, discover it with a read-only tool or ask the user with ask_user.
Before calling a tool that creates or changes something, summarise what you are about to do
and confirm with the user via ask_user.
When the goal is achieved, or cannot be, reply in plain text with a short summary."""


def system_prompt() -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(today=datetime.now(UTC).date().isoformat())


# Turn budget. A turn that involves a human (ask_user, or the confirmation
# gate) cannot run away, so it resets the autonomous counter. Only a streak of
# model-only turns counts towards the limit, and reaching it asks the operator
# rather than aborting. The total ceiling is the last safety net.
MAX_AUTONOMOUS_TURNS = 8
MAX_TOTAL_TURNS = 60
STALL_REPEATS = 3  # same tool + same arguments this many times in a row = stuck


@asynccontextmanager
async def thinking(label: str = "thinking"):
    """Show an animated ``thinking...`` line while waiting, erase it afterwards.

    Only animates on a real terminal. When output is piped to a file there is
    no cursor to move, so nothing is printed and logs stay clean.
    """
    if not sys.stdout.isatty():
        yield
        return

    async def animate() -> None:
        dots = 0
        while True:
            sys.stdout.write(f"\r[agent] {label}{'.' * dots}{' ' * (3 - dots)}")
            sys.stdout.flush()
            dots = (dots + 1) % 4
            await asyncio.sleep(0.4)

    task = asyncio.create_task(animate())
    try:
        yield
    finally:
        task.cancel()
        sys.stdout.write("\r\033[K")  # back to column 0, clear to end of line
        sys.stdout.flush()


def rule(label: str = "") -> None:
    """Horizontal line between turns, with an optional label."""
    width = shutil.get_terminal_size((80, 20)).columns
    text = f" {label} " if label else ""
    print(f"\n{text.center(width, '─')}")


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
    print(f"\n[harness] {entry.name} changes state on the sales agent. Arguments:")
    print(json.dumps(arguments, indent=2))
    reply = await asyncio.to_thread(input, "[user] run it? [y/N] > ")
    return reply.strip().lower() in {"y", "yes"}


def report_error(where: str, exc: BaseException, **context: Any) -> None:
    """Print everything known about a failure, untruncated, in one marked block.

    Used for tool failures (which the run survives) and model failures (which
    end it). ``context`` carries whatever identifies the failing step: tool
    name and arguments, turn number, request payload size.
    """
    print(f"\n[error] {where}")
    print(f"  type:    {type(exc).__module__}.{type(exc).__name__}")
    print(f"  message: {exc}")
    for key, value in context.items():
        text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
        print(
            f"  {key}:"
            + ("\n" + "\n".join("    " + line for line in text.splitlines()) if "\n" in text else f" {text}")
        )
    # Gemini errors carry an HTTP status and a JSON body worth seeing in full.
    for attr in ("code", "status", "details", "response_json"):
        value = getattr(exc, attr, None)
        if value not in (None, "", {}):
            print(
                f"  {attr}: {json.dumps(value, indent=2, default=str) if not isinstance(value, str | int) else value}"
            )
    if exc.__cause__ is not None:
        print(f"  caused by: {type(exc.__cause__).__name__}: {exc.__cause__}")
    if not isinstance(exc, ToolError | errors.APIError):
        # Unexpected exception types: the traceback is the only real clue.
        print("  traceback:")
        for line in traceback.format_exception(exc):
            print("    " + line.rstrip())


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
        # The sales agent rejected or failed the call. Expected during testing.
        report_error(f"tool '{name}' failed on the sales agent", exc, tool=name, arguments=arguments)
        return {"error": str(exc)}
    except Exception as exc:
        # Anything else: transport, a bug in a local tool, a bad result shape.
        # Still returned to the model as data, but with the full traceback shown.
        report_error(f"tool '{name}' raised unexpectedly in the harness", exc, tool=name, arguments=arguments)
        return {"error": f"{type(exc).__name__}: {exc}"}


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


def involves_human(registry: dict[str, ToolEntry], name: str) -> bool:
    entry = registry.get(name)
    return name == "ask_user" or bool(entry and entry.requires_confirmation)


EXIT_WORDS = {"no", "n", "exit", "quit", "q", "done", "bye"}


async def ask_follow_up() -> str:
    """After the model's final answer, ask the operator for a follow-up.

    The conversation history is kept, so the next goal can refer to anything
    said so far. An empty line or an exit word ends the session.
    """
    print("\n[harness] Anything else? (Enter or 'exit' to finish)")
    reply = (await asyncio.to_thread(input, "[user] > ")).strip()
    return "" if reply.lower() in EXIT_WORDS else reply


async def grant_more_turns(streak: int, recent: list[str]) -> bool:
    print(f"\n[harness] {streak} model turns in a row without involving you. Recent calls:")
    for line in recent[-5:]:
        print(f"  {line}")
    reply = await asyncio.to_thread(input, f"[user] allow {MAX_AUTONOMOUS_TURNS} more? [y/N] > ")
    return reply.strip().lower() in {"y", "yes"}


async def run_agent(
    registry: dict[str, ToolEntry],
    goal: str,
    show_history: bool = False,
) -> str:
    """The agent loop: model proposes, harness executes, result goes back. Repeat.

    When the model replies with text the operator is asked for a follow-up,
    which continues the same history. Ends when the operator has nothing more,
    when they decline more autonomous turns, when the same call repeats STALL_REPEATS times in a row,
    or at the absolute ceiling. All of these live here, not in the prompt.

    Tool schemas go to Gemini exactly as the sales agent serves them. If Gemini
    rejects the request, the error propagates: that is a test result.
    """
    client = llm.make_client()
    gemini_tools = [entry.to_gemini() for entry in registry.values()]
    history: list[types.Content] = [llm.user_turn(goal)]
    system = system_prompt()

    tracker = usage.Tracker()
    try:
        return await _loop(client, registry, gemini_tools, history, system, tracker, show_history)
    finally:
        tracker.print_session_line(llm.model_name())


async def _loop(  # noqa: PLR0913
    client: genai.Client,
    registry: dict[str, ToolEntry],
    gemini_tools: list[types.Tool],
    history: list[types.Content],
    system: str,
    tracker: usage.Tracker,
    show_history: bool,
) -> str:
    autonomous_streak = 0
    recent_calls: list[str] = []
    last_signature: str | None = None
    repeats = 0

    for turn in itertools.count(1):
        if turn > MAX_TOTAL_TURNS:
            return f"(stopped: absolute ceiling of {MAX_TOTAL_TURNS} turns reached)"
        if autonomous_streak >= MAX_AUTONOMOUS_TURNS:
            if not await grant_more_turns(autonomous_streak, recent_calls):
                return "(stopped by the operator at the autonomous turn budget)"
            autonomous_streak = 0

        if show_history:
            print_history(history)
        try:
            async with thinking():
                response = await llm.step(client, history, gemini_tools, system)
        except errors.APIError as exc:
            report_error(
                "model call failed",
                exc,
                turn=turn,
                model=llm.model_name(),
                history_turns=len(history),
                tools=[e.name for e in registry.values()],
                tool_schema_bytes=sum(len(json.dumps(e.input_schema)) for e in registry.values()),
            )
            return f"(stopped: model call failed with {type(exc).__name__} {getattr(exc, 'code', '')})"
        tracker.record(response.usage_metadata)
        if not response.candidates:
            report_error(
                "model returned no candidates",
                RuntimeError("empty response"),
                turn=turn,
                prompt_feedback=getattr(response, "prompt_feedback", None),
            )
            return "(stopped: model returned no candidates)"
        model_turn = response.candidates[0].content
        if model_turn is None:
            report_error("model returned no content", RuntimeError("empty candidate"), turn=turn)
            return "(stopped: model returned no content)"
        history.append(model_turn)

        calls = list(response.function_calls or [])
        if not calls:
            rule("done")
            tracker.print_call_line()
            render.agent_text(response.text or "(model returned no text)")
            tracker.print_goal_summary()
            follow_up = await ask_follow_up()
            if not follow_up:
                return "(session ended by the operator)"
            history.append(llm.user_turn(follow_up))
            tracker.new_goal()
            autonomous_streak = 0
            continue

        rule(f"turn {turn}")
        tracker.print_call_line()
        result_parts: list[types.Part] = []
        human_this_turn = False
        for call in calls:
            if not call.name:
                return "(stopped: model issued a function call without a name)"
            arguments = dict(call.args or {})
            signature = f"{call.name} {json.dumps(arguments, sort_keys=True)}"
            repeats = repeats + 1 if signature == last_signature else 1
            last_signature = signature
            if repeats >= STALL_REPEATS:
                return f"(stopped: {call.name} called with identical arguments {repeats} times in a row)"

            print(f"  -> {signature}")
            recent_calls.append(signature[:120])
            human_this_turn = human_this_turn or involves_human(registry, call.name)
            result = await execute(registry, call.name, arguments)
            print(f"  <- {json.dumps(result)[:500]}")
            result_parts.append(llm.tool_result_part(call.name, result))

        history.append(llm.tool_results_turn(result_parts))
        autonomous_streak = 0 if human_this_turn else autonomous_streak + 1

    return "(unreachable)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the buyer agent against the sales agent.")
    parser.add_argument("goal", nargs="?", help="What the buyer wants. Omit to be asked interactively.")
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

        goal = args.goal
        if not goal:
            rule()
            print("[harness] What should the buyer do?")
            goal = (await asyncio.to_thread(input, "[user] > ")).strip()
        if not goal:
            print("[harness] No goal given, exiting.")
            return

        outcome = await run_agent(registry, goal, show_history=args.show_history)
        print(f"[harness] {outcome}")


if __name__ == "__main__":
    asyncio.run(main())
