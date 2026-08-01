"""Tool system: specs, registry, executor, and built-in tools."""

from hoursx.tools.base import (
    Tool,
    ToolContext,
    ToolInvocation,
    ToolOutcome,
    ToolSpec,
)
from hoursx.tools.executor import ApprovalPending, ToolExecutor
from hoursx.tools.registry import ToolRegistry

__all__ = [
    "ApprovalPending",
    "Tool",
    "ToolContext",
    "ToolExecutor",
    "ToolInvocation",
    "ToolOutcome",
    "ToolRegistry",
    "ToolSpec",
]
