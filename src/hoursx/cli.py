"""HoursX command-line entry point.

Subcommands: ``serve`` (API), ``worker`` (arq background worker), ``db-init``
(create tables), ``create-user`` (bootstrap an account non-interactively).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from hoursx.config import get_settings
from hoursx.observability import configure_logging


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
    from hoursx.db.engine import Database

    async def init() -> None:
        db = Database(get_settings().database_url)
        await db.create_all()
        await db.dispose()

    asyncio.run(init())
    print("database initialized")
    return 0


def _cmd_create_user(args: argparse.Namespace) -> int:
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
    configure_logging(get_settings().log_level, get_settings().log_json)
    parser = argparse.ArgumentParser(prog="hoursx", description="HoursX agent platform")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("serve", help="run the API server").set_defaults(fn=_cmd_serve)
    subparsers.add_parser("worker", help="run the background worker").set_defaults(fn=_cmd_worker)
    subparsers.add_parser("db-init", help="create database tables").set_defaults(fn=_cmd_db_init)

    create_user = subparsers.add_parser("create-user", help="create a user + workspace")
    create_user.add_argument("--email", required=True)
    create_user.add_argument("--password", required=True)
    create_user.add_argument("--name", required=True)
    create_user.set_defaults(fn=_cmd_create_user)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
