"""The "brain" of the harness: a thin wrapper around the Gemini SDK.

Keeping every Gemini-specific call in this one module means swapping the
model provider later touches only this file, not the loop.

Configuration comes from environment variables:
    GEMINI_API_KEY   read by the SDK itself
    GEMINI_MODEL     model id, defaults to a fast Gemini model
"""

import os
from typing import Any

from google import genai
from google.genai import types

DEFAULT_MODEL = "gemini-3.6-flash"


def make_client() -> genai.Client:
    """Build a Gemini client. The SDK reads GEMINI_API_KEY from the environment."""
    return genai.Client()


def model_name() -> str:
    return os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)


async def hello(client: genai.Client) -> str:
    """Send one plain prompt with no tools. Proves auth and SDK setup."""
    response = await client.aio.models.generate_content(
        model=model_name(),
        contents="Say hello in one short sentence and name the model you are.",
    )
    return response.text or ""


def to_gemini_tool(name: str, description: str | None, input_schema: dict[str, Any]) -> types.Tool:
    """Convert one MCP tool definition into a Gemini tool declaration.

    MCP gives us a name, a description and a JSON Schema for the arguments.
    Gemini accepts raw JSON Schema via ``parameters_json_schema``, so no
    translation of the schema itself is needed.
    """
    declaration = types.FunctionDeclaration(
        name=name,
        description=description or "",
        parameters_json_schema=input_schema,
    )
    return types.Tool(function_declarations=[declaration])


def user_turn(text: str) -> types.Content:
    """A user message, used for the initial goal."""
    return types.Content(role="user", parts=[types.Part.from_text(text=text)])


def tool_result_part(name: str, result: Any) -> types.Part:
    """Wrap one tool's output as a function response the model can read.

    Gemini requires a dict here. Sales agent results are usually dicts
    already; anything else (text blocks, error strings) is wrapped.
    """
    payload = result if isinstance(result, dict) else {"result": result}
    return types.Part.from_function_response(name=name, response=payload)


def tool_results_turn(parts: list[types.Part]) -> types.Content:
    """All tool results for one model turn go back together in one user turn."""
    return types.Content(role="user", parts=parts)


async def step(
    client: genai.Client,
    history: list[types.Content],
    tools: list[types.Tool],
    system: str,
) -> types.GenerateContentResponse:
    # list is invariant, so list[Content] is not a list[ContentUnion]; rebuild it.
    contents: list[types.ContentUnion] = list(history)
    return await client.aio.models.generate_content(
        model=model_name(),
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system,
            tools=tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
