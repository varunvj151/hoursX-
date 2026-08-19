"""Argument parsing and dispatch for the ``hoursx`` command."""

from __future__ import annotations

import argparse
import sys

from hoursx import __version__
from hoursx.cli import commands
from hoursx.config import get_settings
from hoursx.observability import configure_logging


def _add_server_commands(subparsers: argparse._SubParsersAction) -> None:
    serve = subparsers.add_parser("serve", help="run the API and WebSocket gateway")
    serve.set_defaults(fn=_cmd_serve, needs_async=False)

    worker = subparsers.add_parser("worker", help="run the background worker")
    worker.set_defaults(fn=_cmd_worker, needs_async=False)

    db_init = subparsers.add_parser("db-init", help="create database tables")
    db_init.set_defaults(fn=_cmd_db_init, needs_async=False)

    create_user = subparsers.add_parser("create-user", help="create a user and workspace")
    create_user.add_argument("--email", required=True)
    create_user.add_argument("--password", required=True)
    create_user.add_argument("--name", required=True)
    create_user.set_defaults(fn=_cmd_create_user, needs_async=False)


def _add_agent_commands(subparsers: argparse._SubParsersAction) -> None:
    agent = subparsers.add_parser("agent", help="create, list, and run agents")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)

    run = agent_sub.add_parser("run", help="run one goal to completion")
    run.add_argument("goal", help="what the agent should accomplish")
    run.add_argument("--agent", default="operator", help="agent handle (default: operator)")
    run.add_argument("--quiet", action="store_true", help="suppress per-tool progress lines")
    run.set_defaults(fn=commands.cmd_agent_run)

    listing = agent_sub.add_parser("list", help="list agent profiles")
    listing.set_defaults(fn=commands.cmd_agent_list)

    create = agent_sub.add_parser("create", help="create an agent profile")
    create.add_argument("handle")
    create.add_argument("--title", default="")
    create.add_argument("--instructions", default="")
    create.add_argument("--model", default="deep", help="model alias (fast|deep)")
    create.add_argument("--tools", default="", help="comma-separated tool grant globs")
    create.set_defaults(fn=commands.cmd_agent_create)


def _add_run_commands(subparsers: argparse._SubParsersAction) -> None:
    run = subparsers.add_parser("run", help="inspect and control runs")
    run_sub = run.add_subparsers(dest="run_command", required=True)

    listing = run_sub.add_parser("list", help="recent runs")
    listing.add_argument("--limit", type=int, default=20)
    listing.set_defaults(fn=commands.cmd_run_list)

    show = run_sub.add_parser("show", help="run detail with its step trace")
    show.add_argument("run_id", help="run id or unique prefix")
    show.set_defaults(fn=commands.cmd_run_show)

    cancel = run_sub.add_parser("cancel", help="request cancellation")
    cancel.add_argument("run_id", help="run id or unique prefix")
    cancel.set_defaults(fn=commands.cmd_run_cancel)


def _add_approval_commands(subparsers: argparse._SubParsersAction) -> None:
    approvals = subparsers.add_parser("approvals", help="the human approval gate")
    approvals_sub = approvals.add_subparsers(dest="approvals_command", required=True)

    listing = approvals_sub.add_parser("list", help="pending approvals")
    listing.set_defaults(fn=commands.cmd_approvals_list)

    approve = approvals_sub.add_parser("approve", help="approve a paused tool call")
    approve.add_argument("approval_id", help="approval id or unique prefix")
    approve.set_defaults(fn=commands.cmd_approvals_approve)

    deny = approvals_sub.add_parser("deny", help="deny a paused tool call")
    deny.add_argument("approval_id", help="approval id or unique prefix")
    deny.set_defaults(fn=commands.cmd_approvals_deny)


def _add_knowledge_commands(subparsers: argparse._SubParsersAction) -> None:
    knowledge = subparsers.add_parser("knowledge", help="retrieval corpus")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command", required=True)

    add = knowledge_sub.add_parser("add", help="ingest a text file")
    add.add_argument("path")
    add.add_argument("--title", default="")
    add.set_defaults(fn=commands.cmd_knowledge_add)

    search = knowledge_sub.add_parser("search", help="search the corpus")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    search.set_defaults(fn=commands.cmd_knowledge_search)


def _add_system_commands(subparsers: argparse._SubParsersAction) -> None:
    system = subparsers.add_parser("system", help="host and kernel inspection")
    system_sub = system.add_subparsers(dest="system_command", required=True)

    probe = system_sub.add_parser("probe", help="kernel, memory, load, and filesystems")
    probe.add_argument("--verbose", "-v", action="store_true", help="include kernel modules")
    probe.set_defaults(fn=commands.cmd_system_probe)

    processes = system_sub.add_parser("processes", help="process table from /proc")
    processes.add_argument("--limit", type=int, default=25)
    processes.add_argument("--sort", default="rss", choices=["rss", "pid"])
    processes.set_defaults(fn=commands.cmd_system_processes)

    kernel_log = system_sub.add_parser("kernel-log", help="kernel ring buffer")
    kernel_log.add_argument("--lines", type=int, default=80)
    kernel_log.set_defaults(fn=commands.cmd_system_kernel_log)


def _add_change_commands(subparsers: argparse._SubParsersAction) -> None:
    changes = subparsers.add_parser("changes", help="host changes made by agents")
    changes_sub = changes.add_subparsers(dest="changes_command", required=True)

    listing = changes_sub.add_parser("list", help="recorded changes and their state")
    listing.add_argument("--limit", type=int, default=25)
    listing.set_defaults(fn=commands.cmd_changes_list)

    revert = changes_sub.add_parser("revert", help="undo a recorded change")
    revert.add_argument("change_id", help="change id or unique prefix")
    revert.set_defaults(fn=commands.cmd_changes_revert)

    confirm = changes_sub.add_parser("confirm", help="keep a change past its revert timer")
    confirm.add_argument("change_id", help="change id or unique prefix")
    confirm.set_defaults(fn=commands.cmd_changes_confirm)


def _add_channel_commands(subparsers: argparse._SubParsersAction) -> None:
    channels = subparsers.add_parser("channels", help="messaging channels and their conversations")
    channels_sub = channels.add_subparsers(dest="channels_command", required=True)

    listing = channels_sub.add_parser("list", help="configured channels, bindings, owed replies")
    listing.add_argument("--limit", type=int, default=25)
    listing.set_defaults(fn=commands.cmd_channels_list)

    register = channels_sub.add_parser("register", help="point Telegram at this deployment")
    register.add_argument(
        "--url", default="", help="public base URL (defaults to HOURSX_PUBLIC_URL)"
    )
    register.set_defaults(fn=commands.cmd_channels_register)

    dispatch = channels_sub.add_parser("dispatch", help="settle replies still owed, once")
    dispatch.set_defaults(fn=commands.cmd_channels_dispatch)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hoursx",
        description="HoursX — autonomous AI agent platform",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  hoursx agent run 'why is disk usage climbing on this host?'\n"
            "  hoursx chat\n"
            "  hoursx system probe -v\n"
            "  hoursx approvals list\n"
            "  hoursx changes list\n"
            "  hoursx channels list\n"
            "  hoursx doctor\n"
            "  hoursx gui\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"hoursx {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_server_commands(subparsers)
    _add_agent_commands(subparsers)
    _add_run_commands(subparsers)
    _add_approval_commands(subparsers)
    _add_knowledge_commands(subparsers)
    _add_system_commands(subparsers)
    _add_change_commands(subparsers)
    _add_channel_commands(subparsers)

    chat = subparsers.add_parser("chat", help="interactive agent session")
    chat.add_argument("--agent", default="operator")
    chat.set_defaults(fn=commands.cmd_chat)

    sessions = subparsers.add_parser("sessions", help="list conversation sessions")
    sessions.add_argument("--limit", type=int, default=20)
    sessions.set_defaults(fn=commands.cmd_session_list)

    doctor = subparsers.add_parser("doctor", help="diagnose this deployment's configuration")
    doctor.set_defaults(fn=commands.cmd_doctor)

    gui = subparsers.add_parser("gui", help="launch the desktop application")
    gui.set_defaults(fn=commands.cmd_gui)

    return parser


# ----------------------------------------------------- synchronous commands


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "hoursx.api:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    from arq.worker import run_worker

    from hoursx.jobs import worker_settings_class

    run_worker(worker_settings_class())
    return 0


def _cmd_db_init(args: argparse.Namespace) -> int:
    import asyncio

    from hoursx.db.engine import Database

    async def init() -> None:
        db = Database(get_settings().database_url)
        await db.create_all()
        await db.dispose()

    asyncio.run(init())
    print("database initialized")
    return 0


def _cmd_create_user(args: argparse.Namespace) -> int:
    import asyncio

    from sqlalchemy import select

    from hoursx.auth import hash_password
    from hoursx.auth.rbac import Role
    from hoursx.db.engine import Database
    from hoursx.db.models import User, Workspace, WorkspaceMember

    async def create() -> int:
        db = Database(get_settings().database_url)
        await db.create_all()
        try:
            async with db.session() as session:
                existing = (
                    await session.execute(select(User).where(User.email == args.email.lower()))
                ).scalar_one_or_none()
                if existing is not None:
                    print(f"user {args.email} already exists", file=sys.stderr)
                    return 1
                user = User(
                    email=args.email.lower(),
                    display_name=args.name,
                    password_hash=hash_password(args.password),
                )
                session.add(user)
                await session.flush()
                workspace = Workspace(name=f"{args.name}'s workspace", slug=f"ws-{user.id[:12]}")
                session.add(workspace)
                await session.flush()
                session.add(
                    WorkspaceMember(
                        workspace_id=workspace.id, user_id=user.id, role=Role.OWNER.value
                    )
                )
            print(f"created user {args.email}")
            return 0
        finally:
            await db.dispose()

    return asyncio.run(create())


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    # CLI output is for a human at a terminal; JSON logs would bury it.
    configure_logging(settings.log_level, as_json=False)

    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "needs_async", True):
        return commands.run_async(args.fn(args))
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
