"""Request-scoped dependencies: services access, authentication, and RBAC."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select

from hoursx.auth import (
    Permission,
    Role,
    TokenError,
    hash_api_key,
    role_allows,
    verify_token,
)
from hoursx.db.models import ApiKey, User, Workspace, WorkspaceMember
from hoursx.orchestration import Conductor
from hoursx.services import AppServices


def get_services(request: Request) -> AppServices:
    return request.app.state.services


def get_conductor(request: Request) -> Conductor:
    return request.app.state.conductor


@dataclass
class Actor:
    """The authenticated caller resolved to a workspace and role."""

    user: User
    workspace: Workspace
    role: Role


async def _resolve_user(
    services: AppServices,
    authorization: str | None,
    api_key: str | None,
) -> User:
    async with services.db.session() as db:
        if api_key:
            record = (
                await db.execute(
                    select(ApiKey).where(
                        ApiKey.key_hash == hash_api_key(api_key), ApiKey.revoked.is_(False)
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
            user = await db.get(User, record.user_id)
        elif authorization and authorization.lower().startswith("bearer "):
            try:
                user_id = verify_token(authorization[7:], secret=services.settings.jwt_secret)
            except TokenError as exc:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
            user = await db.get(User, user_id)
        else:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing credentials")
        if user is None or not user.is_active:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown or inactive user")
        return user


async def get_actor(
    services: AppServices = Depends(get_services),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    x_workspace_id: str | None = Header(default=None),
) -> Actor:
    """Authenticate the request and bind it to one workspace membership."""
    user = await _resolve_user(services, authorization, x_api_key)
    async with services.db.session() as db:
        stmt = select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)
        if x_workspace_id:
            stmt = stmt.where(WorkspaceMember.workspace_id == x_workspace_id)
        membership = (await db.execute(stmt.order_by(WorkspaceMember.created_at))).scalars().first()
        if membership is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "no workspace membership")
        workspace = await db.get(Workspace, membership.workspace_id)
        assert workspace is not None
        return Actor(user=user, workspace=workspace, role=Role(membership.role))


def require(permission: Permission):
    """Route dependency asserting the actor's role grants *permission*."""

    async def dependency(actor: Actor = Depends(get_actor)) -> Actor:
        if not role_allows(actor.role, permission):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"requires permission {permission.value}"
            )
        return actor

    return dependency
