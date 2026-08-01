"""Memory manager: working, episodic, and semantic memory.

- **Working memory** is a per-run scratchpad the runtime keeps in process.
- **Episodic memory** is the persisted conversation, replayed under a budget.
- **Semantic memory** is embedding-indexed long-term notes in ``memory_items``,
  scored by cosine similarity weighted by stored importance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hoursx.db.models import MemoryItem, Message
from hoursx.providers.hashing import cosine_similarity
from hoursx.providers.router import ModelRouter
from hoursx.providers.types import ChatMessage, ChatRole


@dataclass
class WorkingMemory:
    """Per-run scratchpad. Discarded when the run ends; notes are surfaced back
    into context on every model call within the run."""

    notes: list[str] = field(default_factory=list)
    max_notes: int = 40

    def jot(self, note: str) -> None:
        self.notes.append(note)
        if len(self.notes) > self.max_notes:
            del self.notes[0 : len(self.notes) - self.max_notes]


@dataclass
class RecalledMemory:
    text: str
    score: float


class MemoryManager:
    """Facade over episodic replay and semantic store/recall."""

    def __init__(self, router: ModelRouter) -> None:
        self._router = router

    # ---------------------------------------------------------------- episodic

    async def conversation_history(
        self, session: AsyncSession, session_id: str, limit: int = 200
    ) -> list[ChatMessage]:
        """Replay persisted messages as chat messages, oldest first."""
        rows = (
            (
                await session.execute(
                    select(Message)
                    .where(Message.session_id == session_id)
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        history: list[ChatMessage] = []
        for row in reversed(rows):
            role = ChatRole(row.role) if row.role in ChatRole._value2member_map_ else ChatRole.USER
            if role == ChatRole.TOOL:
                # Tool transcripts are re-fed as summarized context, not raw
                # protocol messages — providers reject orphaned tool results.
                history.append(
                    ChatMessage(role=ChatRole.USER, content=f"[tool result] {row.content}")
                )
            else:
                history.append(ChatMessage(role=role, content=row.content))
        return history

    # ---------------------------------------------------------------- semantic

    async def remember(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        text: str,
        agent_profile_id: str | None = None,
        importance: float = 0.5,
    ) -> MemoryItem:
        """Store one long-term note with its embedding."""
        [embedding] = await self._router.embed([text])
        item = MemoryItem(
            workspace_id=workspace_id,
            agent_profile_id=agent_profile_id,
            text=text,
            embedding=embedding,
            importance=max(0.0, min(1.0, importance)),
        )
        session.add(item)
        await session.flush()
        return item

    async def recall(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        query: str,
        agent_profile_id: str | None = None,
        top_k: int = 5,
        min_score: float = 0.1,
    ) -> list[RecalledMemory]:
        """Return the best-matching notes for *query*, best first.

        Brute-force cosine over the workspace's notes: correct and simple at the
        scale of curated memory. Swap the scan for a vector index only when a
        deployment actually accumulates enough notes to hurt.
        """
        [query_vec] = await self._router.embed([query])
        stmt = select(MemoryItem).where(MemoryItem.workspace_id == workspace_id)
        if agent_profile_id is not None:
            stmt = stmt.where(
                (MemoryItem.agent_profile_id == agent_profile_id)
                | (MemoryItem.agent_profile_id.is_(None))
            )
        rows = (await session.execute(stmt)).scalars().all()
        scored = [
            RecalledMemory(
                text=row.text,
                score=cosine_similarity(query_vec, row.embedding) * (0.5 + row.importance / 2),
            )
            for row in rows
        ]
        scored = [entry for entry in scored if entry.score >= min_score]
        scored.sort(key=lambda entry: entry.score, reverse=True)
        return scored[:top_k]
