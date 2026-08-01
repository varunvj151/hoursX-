"""Prompt templates and token-budgeted context assembly.

The context builder turns (agent instructions, recalled memory, retrieved
knowledge, conversation history, goal) into a bounded message list. History is
trimmed oldest-first so the newest exchange always survives; auxiliary sections
get hard caps so no single source can flood the window.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hoursx.providers.types import ChatMessage, ChatRole

SYSTEM_TEMPLATE = """\
You are {title}, an autonomous agent on the HoursX platform.

{instructions}

Operating rules:
- Work step by step toward the user's goal; use tools when they help.
- When a tool fails, read its message and adjust — do not repeat a failed call verbatim.
- Give a clear final answer when the goal is met; say plainly if it cannot be met.\
"""

MEMORY_HEADER = "Relevant long-term memory:"
KNOWLEDGE_HEADER = "Relevant knowledge base excerpts:"


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token). Used only for budgeting, so a
    consistent overestimate is preferable to a slow exact count."""
    return max(1, len(text) // 4)


def render_system_prompt(title: str, instructions: str) -> str:
    return SYSTEM_TEMPLATE.format(title=title, instructions=instructions.strip() or "(none)")


@dataclass
class ContextBuilder:
    """Assemble the message list for one model call, within ``token_budget``."""

    token_budget: int = 24_000
    aux_section_budget: int = 2_000  # each of memory / knowledge

    system_prompt: str = ""
    memory_notes: list[str] = field(default_factory=list)
    knowledge_excerpts: list[str] = field(default_factory=list)
    history: list[ChatMessage] = field(default_factory=list)

    def _bounded_section(self, header: str, items: list[str]) -> str | None:
        if not items:
            return None
        lines: list[str] = [header]
        spent = estimate_tokens(header)
        for item in items:
            cost = estimate_tokens(item)
            if spent + cost > self.aux_section_budget:
                break
            lines.append(f"- {item}")
            spent += cost
        return "\n".join(lines) if len(lines) > 1 else None

    def build(self) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        system_parts = [self.system_prompt] if self.system_prompt else []
        for header, items in (
            (MEMORY_HEADER, self.memory_notes),
            (KNOWLEDGE_HEADER, self.knowledge_excerpts),
        ):
            if section := self._bounded_section(header, items):
                system_parts.append(section)
        if system_parts:
            messages.append(ChatMessage(role=ChatRole.SYSTEM, content="\n\n".join(system_parts)))

        fixed_cost = sum(estimate_tokens(m.content) for m in messages)
        remaining = self.token_budget - fixed_cost

        # Walk history newest-first, keep what fits, then restore order.
        kept: list[ChatMessage] = []
        for msg in reversed(self.history):
            cost = estimate_tokens(msg.content) + 8  # role/tool-call overhead
            if remaining - cost < 0 and kept:
                break
            kept.append(msg)
            remaining -= cost
        messages.extend(reversed(kept))
        return messages
