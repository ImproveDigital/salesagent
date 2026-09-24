"""The tool registry: one table of every tool the model may call.

Two kinds of tool end up here with the same shape:
  - sales agent tools, fetched over MCP and executed by the sales agent
  - local tools such as ask_user, executed by the harness itself

The loop only ever looks a name up here and calls ``run``. It never knows or
cares where a tool lives. Safety flags also live here, not in the prompt:
``requires_confirmation`` marks tools that change state on the sales agent, so
the harness can stop and ask a human before running them, whatever the model
intended.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastmcp.client import Client
from google.genai import types

from buyer_agent.local_tools import LocalTool

# Sales agent tools that create or change something. Everything else is read-only.
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "create_media_buy",
        "update_media_buy",
        "sync_creatives",
        "sync_accounts",
        "provide_performance_feedback",
    }
)


@dataclass(frozen=True)
class ToolEntry:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[dict[str, Any]], Awaitable[Any]]
    source: str  # "mcp" or "local", for logging
    requires_confirmation: bool = False

    def to_gemini(self) -> types.Tool:
        """Declare this tool to Gemini with the schema exactly as the sales agent serves it.

        No rewriting on this side: the harness stands in for a real buyer, and a
        real buyer does not repair our schemas. If Gemini rejects a schema, that
        is a finding about the sales agent and must fail loudly here.
        """
        declaration = types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema=self.input_schema,
        )
        return types.Tool(function_declarations=[declaration])


def _mcp_runner(mcp: Client, name: str) -> Callable[[dict[str, Any]], Awaitable[Any]]:
    """Build the ``run`` function for one sales agent tool.

    MCP returns results in two forms: ``structured_content`` (a dict, when the
    tool declares an output schema) or a list of content blocks (text). We
    prefer the structured form and fall back to the text blocks.
    """

    async def run(arguments: dict[str, Any]) -> Any:
        result = await mcp.call_tool(name, arguments)
        if result.structured_content is not None:
            return result.structured_content
        return [block.text for block in result.content if hasattr(block, "text")]

    return run


# tool registry: mcp and local
async def build_registry(
    mcp: Client,
    local_tools: dict[str, LocalTool],
    allow: set[str] | None = None,
) -> dict[str, ToolEntry]:
    registry: dict[str, ToolEntry] = {}

    for tool in await mcp.list_tools():
        if allow is not None and tool.name not in allow:
            continue
        registry[tool.name] = ToolEntry(
            name=tool.name,
            description=tool.description or "",
            input_schema=tool.input_schema,
            run=_mcp_runner(mcp, tool.name),
            source="mcp",
            requires_confirmation=tool.name in WRITE_TOOLS,
        )

    for local in local_tools.values():
        registry[local.name] = ToolEntry(
            name=local.name,
            description=local.description,
            input_schema=local.input_schema,
            run=local.run,
            source="local",
        )

    return registry
