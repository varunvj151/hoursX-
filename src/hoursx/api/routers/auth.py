"""Registration and login.

Registration creates the user plus a personal workspace where they are owner.
The first user on a fresh install always may register; afterwards open
registration is controlled by ``HOURSX_ALLOW_OPEN_REGISTRATION``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from hoursx.api.deps import Actor, get_actor, get_services
from hoursx.api.schemas import LoginRequest, RegisterRequest, TokenResponse, UserOut
from hoursx.auth import hash_password, issue_token, verify_password
from hoursx.auth.rbac import Role
from hoursx.db.models import User, Workspace, WorkspaceMember
from hoursx.services import AppServices

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _issue(services: AppServices, user_id: str, workspace_id: str) -> TokenResponse:
    token = issue_token(
        user_id=user_id,
        secret=services.settings.jwt_secret,
        ttl_seconds=services.settings.jwt_ttl_seconds,
    )
    return TokenResponse(access_token=token, user_id=user_id, workspace_id=workspace_id)


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest, services: AppServices = Depends(get_services)
) -> TokenResponse:
    async with services.db.session() as db:
        user_count = (await db.execute(select(func.count(User.id)))).scalar_one()
        if user_count > 0 and not services.settings.allow_open_registration:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "registration is closed on this deployment"
            )
        existing = (
            await db.execute(select(User).where(User.email == body.email.lower()))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")

        user = User(
            email=body.email.lower(),
            display_name=body.display_name,
            password_hash=hash_password(body.password),
        )
        db.add(user)
        await db.flush()
        workspace = Workspace(name=f"{body.display_name}'s workspace", slug=f"ws-{user.id[:12]}")
        db.add(workspace)
        await db.flush()
        db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=Role.OWNER.value))
        await db.flush()
        return _issue(services, user.id, workspace.id)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, services: AppServices = Depends(get_services)) -> TokenResponse:
    async with services.db.session() as db:
        user = (
            await db.execute(select(User).where(User.email == body.email.lower()))
        ).scalar_one_or_none()
        if user is None or not verify_password(body.password, user.password_hash):
            # One error for both cases: never reveal whether the email exists.
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
        membership = (
            (
                await db.execute(
                    select(WorkspaceMember)
                    .where(WorkspaceMember.user_id == user.id)
                    .order_by(WorkspaceMember.created_at)
                )
            )
            .scalars()
            .first()
        )
        if membership is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "no workspace membership")
        return _issue(services, user.id, membership.workspace_id)


@router.get("/me", response_model=UserOut)
async def me(actor: Actor = Depends(get_actor)) -> UserOut:
    return UserOut(id=actor.user.id, email=actor.user.email, display_name=actor.user.display_name)
