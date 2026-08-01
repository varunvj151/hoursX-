"""Multi-agent delegation tool.

The runtime binds ``ctx.delegate`` only for profiles with ``can_delegate=True``
*and* grants covering ``agent.delegate`` — otherwise the tool is either invisible
or fails with guidance. Delegation runs a child agent to completion and returns
its final answer, so collaboration composes without a bespoke protocol.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from hoursx.tools.base import FunctionTool, ToolContext, ToolOutcome, ToolSpec
from hoursx.tools.registry import ToolRegistry


class DelegateArgs(BaseModel):
    agent: str = Field(description="Handle of the agent to delegate to, e.g. 'researcher'")
    goal: str = Field(min_length=5, description="A complete, self-contained task statement")


async def _delegate(args: DelegateArgs, ctx: ToolContext) -> ToolOutcome:
    if ctx.delegate is None:
        return ToolOutcome.failure(
            "Delegation is not enabled for this agent. Solve the task yourself."
        )
    try:
        answer = await ctx.delegate(args.agent, args.goal)
    except LookupError:
        return ToolOutcome.failure(f"No agent named {args.agent!r} exists in this workspace.")
    return ToolOutcome.success(f"Agent {args.agent!r} finished.", answer=answer)


def register_delegate_tool(registry: ToolRegistry) -> None:
    registry.register(
        FunctionTool(
            ToolSpec(
                name="agent.delegate",
                description=(
                    "Hand a self-contained sub-task to another agent in this "
                    "workspace and receive its final answer."
                ),
                params_model=DelegateArgs,
                timeout_seconds=1800,  # child runs legitimately take minutes
            ),
            _delegate,
        )
    )
