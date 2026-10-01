"""The user simulator: a second model that plays the human operator.

It answers the buyer agent's ask_user questions from a fact sheet and nothing
else. It has its own system prompt and its own history, so it is a separate
agent from the buyer even when both use the same model. The fact sheet is the
scenario; the persona is how the human talks.

The one rule that matters: never invent. If the fact sheet does not hold the
answer, the simulator says so, and the buyer must cope. Otherwise an eval can
pass because the simulator made up a budget, and nobody notices.

Configuration:
    SIMULATOR_MODEL   model id for the simulator, defaults to the buyer's model
"""

import os

from google import genai
from google.genai import types

from buyer_agent import llm

DEFAULT_PERSONA = "A busy media buyer. Answers briefly and directly, one or two sentences, no pleasantries."

SYSTEM_PROMPT_TEMPLATE = """You are playing a human media buyer who is being asked questions by an AI assistant.
The assistant is helping you buy advertising through a sales platform.

Persona: {persona}

You know ONLY the facts below. Answer each question using them.
If a question asks for something not in the facts, reply exactly: "I don't know, use your judgement."
Never invent IDs, numbers, dates, names or preferences. Never add facts that are not listed.
If the assistant offers choices and the facts do not say which to pick, say you have no preference.
Reply with the answer only, as the human would type it. No explanations of what you are doing.

Facts:
{facts}"""


def model_name() -> str:
    return os.environ.get("SIMULATOR_MODEL", llm.model_name())


class Simulator:
    def __init__(self, facts: dict[str, str], persona: str | None = None) -> None:
        self.facts = facts
        self.persona = persona or DEFAULT_PERSONA
        self._client: genai.Client = llm.make_client()
        self._history: list[types.Content] = []

    def system_prompt(self) -> str:
        lines = "\n".join(f"- {key}: {value}" for key, value in self.facts.items()) or "- (none)"
        return SYSTEM_PROMPT_TEMPLATE.format(persona=self.persona, facts=lines)

    async def answer(self, question: str) -> str:
        """Reply to one question, remembering the exchange so later answers stay consistent."""
        self._history.append(types.Content(role="user", parts=[types.Part.from_text(text=question)]))
        contents: list[types.ContentUnion] = list(self._history)  # list is invariant, so rebuild it
        response = await self._client.aio.models.generate_content(
            model=model_name(),
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=self.system_prompt()),
        )
        reply = (response.text or "").strip() or "I don't know, use your judgement."
        self._history.append(types.Content(role="model", parts=[types.Part.from_text(text=reply)]))
        return reply


def parse_facts(pairs: list[str]) -> dict[str, str]:
    """Turn ``["budget=5000", "currency=USD"]`` into a fact sheet. Values may contain '='."""
    facts = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--fact needs key=value, got {pair!r}")
        facts[key.strip()] = value.strip()
    return facts
