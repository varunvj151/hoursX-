"""Built-in tool behavior through the real executor."""

from pathlib import Path

from hoursx.tools.base import ToolContext, ToolInvocation
from hoursx.tools.builtin import register_builtin_tools
from hoursx.tools.executor import ToolExecutor
from hoursx.tools.registry import ToolRegistry


def _setup(tmp_path: Path) -> tuple[ToolExecutor, ToolContext]:
    registry = ToolRegistry()
    register_builtin_tools(registry)
    return ToolExecutor(registry), ToolContext(
        workspace_id="w", session_id="s", run_id="r", sandbox_dir=tmp_path
    )


def _call(name: str, **arguments) -> ToolInvocation:
    return ToolInvocation(call_id="c", tool_name=name, arguments=arguments)


async def test_fs_write_read_list_roundtrip(tmp_path):
    executor, ctx = _setup(tmp_path)
    grants = ["fs.*"]
    write = await executor.execute(
        _call("fs.write", path="notes/hello.txt", content="hi there"), ctx, grants=grants
    )
    assert write.ok
    read = await executor.execute(_call("fs.read", path="notes/hello.txt"), ctx, grants=grants)
    assert read.ok and read.data["content"] == "hi there"
    listing = await executor.execute(_call("fs.list", path="notes"), ctx, grants=grants)
    assert listing.ok and listing.data["entries"] == ["hello.txt"]


async def test_fs_read_missing_file_guides_model(tmp_path):
    executor, ctx = _setup(tmp_path)
    outcome = await executor.execute(_call("fs.read", path="nope.txt"), ctx, grants=["fs.*"])
    assert not outcome.ok and "fs.list" in outcome.summary


async def test_fs_write_escape_is_blocked(tmp_path):
    executor, ctx = _setup(tmp_path)
    outcome = await executor.execute(
        _call("fs.write", path="../evil.txt", content="x"), ctx, grants=["fs.*"]
    )
    assert not outcome.ok
    assert not (tmp_path.parent / "evil.txt").exists()


async def test_shell_run_captures_output_and_exit(tmp_path):
    executor, ctx = _setup(tmp_path)
    ok = await executor.execute(
        _call("shell.run", command="echo out; echo err 1>&2"), ctx, grants=["shell.run"]
    )
    assert ok.ok and "out" in ok.data["stdout"] and "err" in ok.data["stderr"]
    fail = await executor.execute(_call("shell.run", command="exit 3"), ctx, grants=["shell.run"])
    assert not fail.ok and fail.data["exit_code"] == 3


async def test_shell_denies_privileged_commands(tmp_path):
    executor, ctx = _setup(tmp_path)
    outcome = await executor.execute(
        _call("shell.run", command="sudo rm -rf /"), ctx, grants=["shell.run"]
    )
    assert not outcome.ok and "blocked" in outcome.summary


async def test_shell_timeout_kills_process(tmp_path):
    executor, ctx = _setup(tmp_path)
    outcome = await executor.execute(
        _call("shell.run", command="sleep 5", timeout_seconds=0.2), ctx, grants=["shell.run"]
    )
    assert not outcome.ok and "timed out" in outcome.summary


async def test_code_patch_requires_unique_anchor(tmp_path):
    executor, ctx = _setup(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\ny = 1\n")
    ambiguous = await executor.execute(
        _call("code.patch", path="app.py", find="= 1", replace="= 2"), ctx, grants=["code.*"]
    )
    assert not ambiguous.ok and "2 places" in ambiguous.summary
    patched = await executor.execute(
        _call("code.patch", path="app.py", find="x = 1", replace="x = 9"),
        ctx,
        grants=["code.*"],
    )
    assert patched.ok and (tmp_path / "app.py").read_text() == "x = 9\ny = 1\n"


async def test_git_tools_roundtrip(tmp_path):
    executor, ctx = _setup(tmp_path)
    grants = ["git.*", "shell.run"]
    setup = await executor.execute(
        _call(
            "shell.run",
            command=("git init -q && git config user.email t@t.t && git config user.name T"),
        ),
        ctx,
        grants=grants,
    )
    assert setup.ok
    (tmp_path / "f.txt").write_text("v1")
    commit = await executor.execute(_call("git.commit", message="add f.txt"), ctx, grants=grants)
    assert commit.ok
    status = await executor.execute(_call("git.status"), ctx, grants=grants)
    assert status.ok
    log = await executor.execute(_call("git.log"), ctx, grants=grants)
    assert log.ok and "add f.txt" in log.data["output"]


async def test_git_status_outside_repo_guides_model(tmp_path):
    executor, ctx = _setup(tmp_path)
    outcome = await executor.execute(_call("git.status"), ctx, grants=["git.*"])
    assert not outcome.ok and "git init" in outcome.summary


async def test_delegate_without_binding_fails_gracefully(tmp_path):
    executor, ctx = _setup(tmp_path)
    outcome = await executor.execute(
        _call("agent.delegate", agent="researcher", goal="find the answer"),
        ctx,
        grants=["agent.*"],
    )
    assert not outcome.ok and "not enabled" in outcome.summary
