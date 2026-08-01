"""Knowledge-base and long-term-memory tools.

These tools reach the database through ``ctx.services`` (the app service
container), so they are registered like any other tool but only function when
the runtime binds services into the context.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from hoursx.tools.base import FunctionTool, ToolContext, ToolOutcome, ToolSpec
from hoursx.tools.registry import ToolRegistry


class SearchArgs(BaseModel):
    query: str = Field(min_length=2, description="What to look for")


class RememberArgs(BaseModel):
    note: str = Field(min_length=3, description="Durable fact worth remembering long-term")
    importance: float = Field(default=0.5, ge=0.0, le=1.0)


async def _knowledge_search(args: SearchArgs, ctx: ToolContext) -> ToolOutcome:
    services = ctx.services
    if services is None:
        return ToolOutcome.failure("Knowledge base is unavailable in this context.")
    async with services.db.session() as session:
        hits = await services.knowledge.search(
            session, workspace_id=ctx.workspace_id, query=args.query
        )
    if not hits:
        return ToolOutcome.failure(
            f"No knowledge-base matches for {args.query!r}. "
            f"Try different terms, or answer from your own reasoning."
        )
    return ToolOutcome.success(
        f"{len(hits)} knowledge excerpts for {args.query!r}",
        excerpts=[
            {"document": hit.document_title, "score": hit.score, "text": hit.text} for hit in hits
        ],
    )


async def _memory_save(args: RememberArgs, ctx: ToolContext) -> ToolOutcome:
    services = ctx.services
    if services is None:
        return ToolOutcome.failure("Memory is unavailable in this context.")
    async with services.db.session() as session:
        await services.memory.remember(
            session,
            workspace_id=ctx.workspace_id,
            text=args.note,
            importance=args.importance,
        )
    return ToolOutcome.success("Noted for the long term.")


async def _memory_search(args: SearchArgs, ctx: ToolContext) -> ToolOutcome:
    services = ctx.services
    if services is None:
        return ToolOutcome.failure("Memory is unavailable in this context.")
    async with services.db.session() as session:
        hits = await services.memory.recall(
            session, workspace_id=ctx.workspace_id, query=args.query
        )
    if not hits:
        return ToolOutcome.failure(f"No stored memories match {args.query!r}.")
    return ToolOutcome.success(
        f"{len(hits)} memories for {args.query!r}",
        memories=[{"text": hit.text, "score": round(hit.score, 4)} for hit in hits],
    )


def register_recall_tools(registry: ToolRegistry) -> None:
    registry.register(
        FunctionTool(
            ToolSpec(
                name="knowledge.search",
                description="Search the workspace knowledge base (uploaded documents).",
                params_model=SearchArgs,
            ),
            _knowledge_search,
        )
    )
    registry.register(
        FunctionTool(
            ToolSpec(
                name="memory.save",
                description="Save a durable note to long-term memory.",
                params_model=RememberArgs,
            ),
            _memory_save,
        )
    )
    registry.register(
        FunctionTool(
            ToolSpec(
                name="memory.search",
                description="Search long-term memory for relevant notes.",
                params_model=SearchArgs,
            ),
            _memory_search,
        )
    )
