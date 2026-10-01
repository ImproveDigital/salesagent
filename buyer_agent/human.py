"""The operator, as seen by the harness: every question the harness asks a human goes through here.

Interactive mode reads the keyboard. Batch mode has nobody at the keyboard, so
each question gets a fixed policy instead:
  - ask_user answers come from a script (``--answer``), in order; after that
    from the simulator if one is attached; otherwise the model is told no
    answer is available
  - write tools are approved (the run would be pointless otherwise)
  - the autonomous turn budget is not extended; hitting it ends the run
  - no follow-up is asked; the model's first final answer ends the run

Keeping all of this in one object means the loop never checks ``batch`` itself.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Protocol


class Answerer(Protocol):
    """Anything that can answer a question in place of the keyboard, such as the simulator."""

    async def answer(self, question: str) -> str: ...


@dataclass
class Human:
    batch: bool = False
    answers: list[str] = field(default_factory=list)  # scripted ask_user answers, consumed front to back
    simulator: Answerer | None = None  # answers whatever the script does not cover

    async def _input(self, prompt: str) -> str:
        # input() blocks the thread, so it runs in a worker to keep the event loop free.
        return (await asyncio.to_thread(input, prompt)).strip()

    async def answer(self, question: str) -> str | None:
        """Reply to an ask_user question. ``None`` means nobody can answer (batch, script exhausted)."""
        if not self.batch:
            return await self._input("[user] > ")
        if self.answers:
            reply = self.answers.pop(0)
            print(f"[user] {reply}  (scripted)")
            return reply
        if self.simulator is not None:
            reply = await self.simulator.answer(question)
            print(f"[user] {reply}  (simulated)")
            return reply
        print("[user] (batch: no scripted answer left)")
        return None

    async def yes(self, prompt: str, batch_default: bool) -> bool:
        """A yes/no question. In batch mode the given default is used and shown."""
        if self.batch:
            print(f"{prompt}{'y' if batch_default else 'n'}  (batch)")
            return batch_default
        return (await self._input(prompt)).lower() in {"y", "yes"}

    async def follow_up(self) -> str:
        """After a final answer: the next goal, or empty to finish. Batch always finishes."""
        if self.batch:
            return ""
        print("\n[harness] Anything else? (Enter or 'exit' to finish)")
        reply = await self._input("[user] > ")
        return "" if reply.lower() in EXIT_WORDS else reply


EXIT_WORDS = {"no", "n", "exit", "quit", "q", "done", "bye"}
