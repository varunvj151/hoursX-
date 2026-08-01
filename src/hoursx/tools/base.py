"""Tool contracts.

A tool = a :class:`ToolSpec` (identity + pydantic parameter model + policy flags)
plus an async ``run``. Tools receive validated, typed arguments and a
:class:`ToolContext`; they return a :class:`ToolOutcome` whose ``summary`` is
what the model reads — it must always say what happened and, on failure, what to
try next.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from hoursx.providers.types import ToolDescriptor


@dataclass(frozen=True)
class ToolSpec:
    """Identity and policy for one tool."""

    name: str  # namespaced: "fs.read", "shell.run"
    description: str
    params_model: type[BaseModel]
    requires_approval: bool = False  # human gate before every execution
    timeout_seconds: float | None = None  # override the platform default

    def descriptor(self) -> ToolDescriptor:
        """The provider-facing advertisement of this tool."""
        return ToolDescriptor(
            name=self.name,
            description=self.description,
            parameters=self.params_model.model_json_schema(),
        )


class ToolOutcome(BaseModel):
    """Structured result of a tool execution.

    ``summary`` is model-visible prose; ``data`` is structured payload for both
    the model and the UI. Failures still return an outcome (``ok=False``) so the
    model can react — executor-level faults are the only hard errors.
    """

    ok: bool
    summary: str
    data: dict[str, Any] = {}

    @classmethod
    def success(cls, summary: str, **data: Any) -> ToolOutcome:
        return cls(ok=True, summary=summary, data=data)

    @classmethod
    def failure(cls, summary: str, **data: Any) -> ToolOutcome:
        return cls(ok=False, summary=summary, data=data)


DelegateFn = Callable[[str, str], Awaitable[str]]
"""(agent_handle, goal) -> final answer. Bound by the runtime when delegation is allowed."""


@dataclass
class ToolContext:
    """Everything a tool may touch. Tools never import app singletons; the
    executor hands them this context so tests can substitute any part."""

    workspace_id: str
    session_id: str
    run_id: str
    sandbox_dir: Path
    services: Any = None  # AppServices (router, memory, knowledge, db)
    delegate: DelegateFn | None = None
    working_notes: list[str] = field(default_factory=list)


class ToolInvocation(BaseModel):
    """One requested call, post-validation."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]


@runtime_checkable
class Tool(Protocol):
    spec: ToolSpec

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolOutcome: ...


class FunctionTool:
    """Adapter turning an async function into a :class:`Tool`."""

    def __init__(
        self,
        spec: ToolSpec,
        fn: Callable[[BaseModel, ToolContext], Awaitable[ToolOutcome]],
    ) -> None:
        self.spec = spec
        self._fn = fn

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolOutcome:
        return await self._fn(args, ctx)
