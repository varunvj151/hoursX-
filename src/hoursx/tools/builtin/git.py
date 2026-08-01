"""Git tools operating on the session workspace repository."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from hoursx.tools.base import FunctionTool, ToolContext, ToolOutcome, ToolSpec
from hoursx.tools.registry import ToolRegistry

_OUTPUT_CAP = 20_000


async def _git(sandbox: Path, *argv: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "git",
        *argv,
        cwd=sandbox,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    text = stdout.decode(errors="replace")
    return process.returncode or 0, text[-_OUTPUT_CAP:]


class NoArgs(BaseModel):
    pass


class DiffArgs(BaseModel):
    ref: str = Field(default="", description="Optional ref/path spec, e.g. 'HEAD~1' or a path")


class CommitArgs(BaseModel):
    message: str = Field(min_length=3, description="Commit message")
    add_all: bool = Field(default=True, description="Stage all changes before committing")


async def _status(args: NoArgs, ctx: ToolContext) -> ToolOutcome:
    code, out = await _git(ctx.sandbox_dir, "status", "--short", "--branch")
    if code != 0:
        return ToolOutcome.failure(
            f"git status failed: {out.strip() or 'not a git repository'}. "
            f"Run 'git init' via shell.run first if needed."
        )
    return ToolOutcome.success("git status", output=out)


async def _diff(args: DiffArgs, ctx: ToolContext) -> ToolOutcome:
    argv = ["diff", args.ref] if args.ref else ["diff"]
    code, out = await _git(ctx.sandbox_dir, *argv)
    if code != 0:
        return ToolOutcome.failure(f"git diff failed: {out.strip()}")
    return ToolOutcome.success("git diff" + (f" {args.ref}" if args.ref else ""), output=out)


async def _log(args: NoArgs, ctx: ToolContext) -> ToolOutcome:
    code, out = await _git(ctx.sandbox_dir, "log", "--oneline", "-20")
    if code != 0:
        return ToolOutcome.failure(f"git log failed: {out.strip()}")
    return ToolOutcome.success("last 20 commits", output=out)


async def _commit(args: CommitArgs, ctx: ToolContext) -> ToolOutcome:
    if args.add_all:
        code, out = await _git(ctx.sandbox_dir, "add", "-A")
        if code != 0:
            return ToolOutcome.failure(f"git add failed: {out.strip()}")
    code, out = await _git(ctx.sandbox_dir, "commit", "-m", args.message)
    if code != 0:
        return ToolOutcome.failure(f"git commit failed: {out.strip() or 'nothing to commit'}")
    return ToolOutcome.success("Committed", output=out)


def register_git_tools(registry: ToolRegistry) -> None:
    registry.register(
        FunctionTool(
            ToolSpec(
                name="git.status",
                description="Show git status of the session workspace.",
                params_model=NoArgs,
            ),
            _status,
        )
    )
    registry.register(
        FunctionTool(
            ToolSpec(
                name="git.diff",
                description="Show the git diff of the session workspace.",
                params_model=DiffArgs,
            ),
            _diff,
        )
    )
    registry.register(
        FunctionTool(
            ToolSpec(
                name="git.log",
                description="Show recent commits in the session workspace.",
                params_model=NoArgs,
            ),
            _log,
        )
    )
    registry.register(
        FunctionTool(
            ToolSpec(
                name="git.commit",
                description="Stage and commit changes in the session workspace.",
                params_model=CommitArgs,
            ),
            _commit,
        )
    )
