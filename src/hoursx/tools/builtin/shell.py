"""Terminal execution inside the session sandbox.

The command runs with the sandbox as CWD. A small deny-list blocks commands that
reach outside any sandbox by nature (privilege escalation, host power control);
everything else is permitted *inside* the sandbox — operators who want a human
gate add ``shell.run`` to the executor's force-approval set.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from hoursx.tools.base import FunctionTool, ToolContext, ToolOutcome, ToolSpec
from hoursx.tools.registry import ToolRegistry

_OUTPUT_CAP = 20_000
_DENIED_PREFIXES = ("sudo", "su ", "shutdown", "reboot", "mkfs", "mount", "umount")


class ShellArgs(BaseModel):
    command: str = Field(description="Shell command to run in the session workspace")
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)


def _tail(data: bytes) -> str:
    text = data.decode(errors="replace")
    # Keep the tail — errors and summaries live at the end of output.
    return text[-_OUTPUT_CAP:] if len(text) > _OUTPUT_CAP else text


async def _run(args: ShellArgs, ctx: ToolContext) -> ToolOutcome:
    stripped = args.command.strip()
    if any(stripped.startswith(prefix) for prefix in _DENIED_PREFIXES):
        return ToolOutcome.failure(
            "That command class is blocked by platform policy. "
            "Work within the session workspace instead."
        )
    process = await asyncio.create_subprocess_shell(
        args.command,
        cwd=ctx.sandbox_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=args.timeout_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()
        return ToolOutcome.failure(
            f"Command timed out after {args.timeout_seconds:.0f}s and was killed. "
            f"Run something shorter or raise timeout_seconds."
        )
    exit_code = process.returncode or 0
    summary = f"Exit {exit_code}: {stripped[:120]}"
    outcome = ToolOutcome(
        ok=exit_code == 0,
        summary=summary if exit_code == 0 else summary + " — inspect stderr and adjust.",
        data={"exit_code": exit_code, "stdout": _tail(stdout), "stderr": _tail(stderr)},
    )
    return outcome


def register_shell_tools(registry: ToolRegistry) -> None:
    registry.register(
        FunctionTool(
            ToolSpec(
                name="shell.run",
                description=(
                    "Run a shell command in the session workspace. "
                    "Returns exit code, stdout, and stderr."
                ),
                params_model=ShellArgs,
                timeout_seconds=620,  # outer guard; inner timeout governs the process
            ),
            _run,
        )
    )
