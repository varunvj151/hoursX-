"""Tool executor: the policy envelope around every tool call.

Responsibilities, in order: grant check → argument validation → approval gate →
timeout-bounded execution → structured outcome. The executor never raises for a
tool's own failure — the model gets an ``ok=False`` outcome with guidance — but
it *does* signal :class:`ApprovalPending` so the runtime can suspend the run.
"""

from __future__ import annotations

import asyncio

from pydantic import ValidationError

from hoursx.observability import get_logger, metrics
from hoursx.tools.base import Tool, ToolContext, ToolInvocation, ToolOutcome
from hoursx.tools.registry import ToolRegistry

log = get_logger("tools.executor")


class ApprovalPending(Exception):
    """Raised when a call needs a human decision before it may execute.

    Not an error: the runtime catches this, persists an approval request, and
    parks the run until someone decides.
    """

    def __init__(self, invocation: ToolInvocation, reason: str) -> None:
        super().__init__(reason)
        self.invocation = invocation
        self.reason = reason


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        default_timeout: float = 120.0,
        force_approval: frozenset[str] = frozenset(),
    ) -> None:
        self._registry = registry
        self._default_timeout = default_timeout
        # Operator policy: tool names gated regardless of their spec flag.
        self._force_approval = force_approval

    def _needs_approval(self, tool: Tool) -> bool:
        return tool.spec.requires_approval or tool.spec.name in self._force_approval

    async def execute(
        self,
        invocation: ToolInvocation,
        ctx: ToolContext,
        *,
        grants: list[str],
        approved: bool = False,
    ) -> ToolOutcome:
        """Run one validated call. Set ``approved=True`` only when resuming a
        call a human explicitly approved."""
        name = invocation.tool_name
        if not self._registry.is_granted(name, grants):
            metrics.incr("tools.denied")
            return ToolOutcome.failure(
                f"Tool '{name}' is not available to this agent. "
                f"Use one of your listed tools instead."
            )
        tool = self._registry.get(name)
        assert tool is not None  # is_granted implies presence

        try:
            args = tool.spec.params_model.model_validate(invocation.arguments)
        except ValidationError as exc:
            metrics.incr("tools.invalid_args")
            issues = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
            )
            return ToolOutcome.failure(
                f"Invalid arguments for '{name}': {issues}. Fix the arguments and retry."
            )

        if self._needs_approval(tool) and not approved:
            raise ApprovalPending(invocation, f"'{name}' requires human approval")

        timeout = tool.spec.timeout_seconds or self._default_timeout
        try:
            outcome = await asyncio.wait_for(tool.run(args, ctx), timeout=timeout)
        except TimeoutError:
            metrics.incr("tools.timeout")
            return ToolOutcome.failure(
                f"'{name}' timed out after {timeout:.0f}s. "
                f"Try a smaller operation or split the work."
            )
        except Exception:
            metrics.incr("tools.crashed")
            log.exception("tool %s crashed", name)
            return ToolOutcome.failure(f"'{name}' failed unexpectedly. Try a different approach.")
        metrics.incr("tools.ok" if outcome.ok else "tools.failed")
        return outcome
