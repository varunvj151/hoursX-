"""Tool registry gating, executor policy envelope, and sandbox confinement."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from hoursx.tools.base import FunctionTool, ToolContext, ToolInvocation, ToolOutcome, ToolSpec
from hoursx.tools.builtin.fs import SandboxViolation, resolve_in_sandbox
from hoursx.tools.executor import ApprovalPending, ToolExecutor
from hoursx.tools.registry import ToolRegistry


class _Args(BaseModel):
    value: int


def _make_tool(name: str = "demo.add", *, requires_approval: bool = False) -> FunctionTool:
    async def run(args: _Args, ctx: ToolContext) -> ToolOutcome:
        return ToolOutcome.success(f"got {args.value}", doubled=args.value * 2)

    return FunctionTool(
        ToolSpec(
            name=name,
            description="test tool",
            params_model=_Args,
            requires_approval=requires_approval,
        ),
        run,
    )


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace_id="w", session_id="s", run_id="r", sandbox_dir=tmp_path)


def _invocation(name: str, **arguments) -> ToolInvocation:
    return ToolInvocation(call_id="c1", tool_name=name, arguments=arguments)


# ---------------------------------------------------------------------- registry


def test_registry_grants_filter_visibility():
    registry = ToolRegistry()
    registry.register(_make_tool("fs.read"))
    registry.register(_make_tool("shell.run"))
    granted = registry.granted(["fs.*"])
    assert [tool.spec.name for tool in granted] == ["fs.read"]
    assert registry.is_granted("fs.read", ["fs.*"])
    assert not registry.is_granted("shell.run", ["fs.*"])


def test_registry_rejects_duplicates():
    registry = ToolRegistry()
    registry.register(_make_tool())
    with pytest.raises(ValueError):
        registry.register(_make_tool())


def test_registry_descriptor_order_is_deterministic():
    registry = ToolRegistry()
    registry.register(_make_tool("b.tool"))
    registry.register(_make_tool("a.tool"))
    names = [d.name for d in registry.descriptors(["*"])]
    assert names == sorted(names)


# ---------------------------------------------------------------------- executor


async def test_executor_runs_granted_tool(tmp_path):
    registry = ToolRegistry()
    registry.register(_make_tool())
    executor = ToolExecutor(registry)
    outcome = await executor.execute(
        _invocation("demo.add", value=21), _ctx(tmp_path), grants=["demo.*"]
    )
    assert outcome.ok and outcome.data["doubled"] == 42


async def test_executor_denies_ungranted_tool(tmp_path):
    registry = ToolRegistry()
    registry.register(_make_tool())
    executor = ToolExecutor(registry)
    outcome = await executor.execute(
        _invocation("demo.add", value=1), _ctx(tmp_path), grants=["fs.*"]
    )
    assert not outcome.ok and "not available" in outcome.summary


async def test_executor_reports_invalid_arguments(tmp_path):
    registry = ToolRegistry()
    registry.register(_make_tool())
    executor = ToolExecutor(registry)
    outcome = await executor.execute(
        _invocation("demo.add", value="not-a-number"), _ctx(tmp_path), grants=["demo.*"]
    )
    assert not outcome.ok and "Invalid arguments" in outcome.summary


async def test_executor_raises_approval_gate(tmp_path):
    registry = ToolRegistry()
    registry.register(_make_tool(requires_approval=True))
    executor = ToolExecutor(registry)
    with pytest.raises(ApprovalPending):
        await executor.execute(_invocation("demo.add", value=1), _ctx(tmp_path), grants=["demo.*"])
    outcome = await executor.execute(
        _invocation("demo.add", value=1), _ctx(tmp_path), grants=["demo.*"], approved=True
    )
    assert outcome.ok


async def test_executor_force_approval_policy(tmp_path):
    registry = ToolRegistry()
    registry.register(_make_tool())
    executor = ToolExecutor(registry, force_approval=frozenset({"demo.add"}))
    with pytest.raises(ApprovalPending):
        await executor.execute(_invocation("demo.add", value=1), _ctx(tmp_path), grants=["demo.*"])


async def test_executor_times_out(tmp_path):
    import asyncio

    class SlowArgs(BaseModel):
        pass

    async def slow(args: SlowArgs, ctx: ToolContext) -> ToolOutcome:
        await asyncio.sleep(5)
        return ToolOutcome.success("never")

    registry = ToolRegistry()
    registry.register(
        FunctionTool(
            ToolSpec(
                name="demo.slow",
                description="",
                params_model=SlowArgs,
                timeout_seconds=0.05,
            ),
            slow,
        )
    )
    executor = ToolExecutor(registry)
    outcome = await executor.execute(_invocation("demo.slow"), _ctx(tmp_path), grants=["*"])
    assert not outcome.ok and "timed out" in outcome.summary


async def test_executor_captures_tool_crash(tmp_path):
    class CrashArgs(BaseModel):
        pass

    async def crash(args: CrashArgs, ctx: ToolContext) -> ToolOutcome:
        raise RuntimeError("kaboom")

    registry = ToolRegistry()
    registry.register(
        FunctionTool(ToolSpec(name="demo.crash", description="", params_model=CrashArgs), crash)
    )
    executor = ToolExecutor(registry)
    outcome = await executor.execute(_invocation("demo.crash"), _ctx(tmp_path), grants=["*"])
    assert not outcome.ok and "kaboom" not in outcome.summary  # no stack traces to the model


# ----------------------------------------------------------------------- sandbox


def test_sandbox_allows_inside_paths(tmp_path):
    assert resolve_in_sandbox(tmp_path, "a/b.txt") == (tmp_path / "a/b.txt").resolve()


@pytest.mark.parametrize("escape", ["../outside.txt", "../../etc/passwd", "a/../../up"])
def test_sandbox_blocks_traversal(tmp_path, escape):
    with pytest.raises(SandboxViolation):
        resolve_in_sandbox(tmp_path, escape)


def test_sandbox_blocks_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "link").symlink_to(outside)
    with pytest.raises(SandboxViolation):
        resolve_in_sandbox(sandbox, "link/file.txt")
