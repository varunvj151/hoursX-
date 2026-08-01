"""Filesystem tools, confined to the session sandbox.

Every path is resolved (symlinks included) and must land inside the sandbox
root; escapes fail before any I/O happens. This is the single enforcement point
other file-touching tools reuse.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from hoursx.tools.base import FunctionTool, ToolContext, ToolOutcome, ToolSpec
from hoursx.tools.registry import ToolRegistry

_MAX_READ_CHARS = 60_000


class SandboxViolation(Exception):
    """A path escaped the session sandbox."""


def resolve_in_sandbox(sandbox: Path, relative: str) -> Path:
    """Resolve *relative* inside *sandbox*; raise on any escape attempt."""
    root = sandbox.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise SandboxViolation(f"path {relative!r} escapes the session workspace")
    return candidate


class ReadArgs(BaseModel):
    path: str = Field(description="File path relative to the session workspace")
    offset: int = Field(default=0, ge=0, description="Character offset to start from")


class WriteArgs(BaseModel):
    path: str = Field(description="File path relative to the session workspace")
    content: str = Field(description="Full content to write (parent dirs are created)")


class ListArgs(BaseModel):
    path: str = Field(default=".", description="Directory relative to the session workspace")


async def _read(args: ReadArgs, ctx: ToolContext) -> ToolOutcome:
    try:
        target = resolve_in_sandbox(ctx.sandbox_dir, args.path)
        text = target.read_text(errors="replace")
    except SandboxViolation as exc:
        return ToolOutcome.failure(str(exc))
    except FileNotFoundError:
        return ToolOutcome.failure(f"{args.path} does not exist. Use fs.list to explore.")
    except IsADirectoryError:
        return ToolOutcome.failure(f"{args.path} is a directory. Use fs.list instead.")
    window = text[args.offset : args.offset + _MAX_READ_CHARS]
    truncated = args.offset + len(window) < len(text)
    note = f" (truncated; continue with offset={args.offset + len(window)})" if truncated else ""
    return ToolOutcome.success(
        f"Read {len(window)} chars from {args.path}{note}",
        content=window,
        truncated=truncated,
    )


async def _write(args: WriteArgs, ctx: ToolContext) -> ToolOutcome:
    try:
        target = resolve_in_sandbox(ctx.sandbox_dir, args.path)
    except SandboxViolation as exc:
        return ToolOutcome.failure(str(exc))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(args.content)
    return ToolOutcome.success(f"Wrote {len(args.content)} chars to {args.path}")


async def _list(args: ListArgs, ctx: ToolContext) -> ToolOutcome:
    try:
        target = resolve_in_sandbox(ctx.sandbox_dir, args.path)
    except SandboxViolation as exc:
        return ToolOutcome.failure(str(exc))
    if not target.exists():
        return ToolOutcome.failure(f"{args.path} does not exist.")
    if not target.is_dir():
        return ToolOutcome.failure(f"{args.path} is a file. Use fs.read instead.")
    entries = sorted(
        f"{entry.name}/" if entry.is_dir() else entry.name for entry in target.iterdir()
    )[:500]
    return ToolOutcome.success(f"{len(entries)} entries in {args.path}", entries=entries)


def register_fs_tools(registry: ToolRegistry) -> None:
    registry.register(
        FunctionTool(
            ToolSpec(
                name="fs.read",
                description="Read a text file from the session workspace.",
                params_model=ReadArgs,
            ),
            _read,
        )
    )
    registry.register(
        FunctionTool(
            ToolSpec(
                name="fs.write",
                description="Create or overwrite a text file in the session workspace.",
                params_model=WriteArgs,
            ),
            _write,
        )
    )
    registry.register(
        FunctionTool(
            ToolSpec(
                name="fs.list",
                description="List a directory in the session workspace.",
                params_model=ListArgs,
            ),
            _list,
        )
    )
