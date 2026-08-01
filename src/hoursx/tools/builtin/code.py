"""Code-editing tool: exact-match search/replace patching.

Whole-file rewrites (``fs.write``) lose concurrent edits and burn tokens;
``code.patch`` applies a targeted edit and fails loudly when the anchor text is
missing or ambiguous, which is exactly the feedback a model needs to re-read the
file and retry.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from hoursx.tools.base import FunctionTool, ToolContext, ToolOutcome, ToolSpec
from hoursx.tools.builtin.fs import SandboxViolation, resolve_in_sandbox
from hoursx.tools.registry import ToolRegistry


class PatchArgs(BaseModel):
    path: str = Field(description="File to edit, relative to the session workspace")
    find: str = Field(min_length=1, description="Exact text to replace (must match once)")
    replace: str = Field(description="Replacement text")


async def _patch(args: PatchArgs, ctx: ToolContext) -> ToolOutcome:
    try:
        target = resolve_in_sandbox(ctx.sandbox_dir, args.path)
        text = target.read_text()
    except SandboxViolation as exc:
        return ToolOutcome.failure(str(exc))
    except FileNotFoundError:
        return ToolOutcome.failure(f"{args.path} does not exist. Create it with fs.write.")
    count = text.count(args.find)
    if count == 0:
        return ToolOutcome.failure(
            f"The 'find' text was not found in {args.path}. "
            f"Read the file again and match the current content exactly."
        )
    if count > 1:
        return ToolOutcome.failure(
            f"The 'find' text matches {count} places in {args.path}. "
            f"Include more surrounding context to make it unique."
        )
    target.write_text(text.replace(args.find, args.replace, 1))
    return ToolOutcome.success(f"Patched {args.path}.")


def register_code_tools(registry: ToolRegistry) -> None:
    registry.register(
        FunctionTool(
            ToolSpec(
                name="code.patch",
                description=(
                    "Edit a file by replacing one exact text match. "
                    "Safer and cheaper than rewriting the whole file."
                ),
                params_model=PatchArgs,
            ),
            _patch,
        )
    )
