"""Workspace membership and role administration.

Guards that make this safe to expose:

- You cannot escalate anyone above your own role. An admin promoting someone to
  owner would be privilege escalation by proxy.
- The last owner cannot be demoted or removed, so a workspace can never become
  unadministrable.
- Every change is audited.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select

from hoursx.api.deps import Actor, get_services, require
from hoursx.audit import AuditAction, record
from hoursx.auth import Permission
from hoursx.auth.rbac import Role
from hoursx.db.models import User, WorkspaceMember
from hoursx.errors import ConflictError, NotFoundError, ValidationError
from hoursx.services import AppServices

router = APIRouter(prefix="/v1/members", tags=["members"])

# Higher index = more authority; used for the no-escalation check.
_RANK = {Role.VIEWER: 0, Role.MEMBER: 1, Role.ADMIN: 2, Role.OWNER: 3}


class MemberOut(BaseModel):
    user_id: str
    email: str
    display_name: str
    role: str
    created_at: datetime


class MemberInvite(BaseModel):
    email: EmailStr
    role: Role = Role.MEMBER


class MemberRoleUpdate(BaseModel):
    role: Role


def _assert_no_escalation(actor: Actor, target_role: Role) -> None:
    if _RANK[target_role] > _RANK[actor.role]:
        raise ValidationError(
            f"cannot grant {target_role.value}: it outranks your own role ({actor.role.value})"
        )


async def _owner_count(db, workspace_id: str) -> int:
    return int(
        (
            await db.execute(
                select(func.count(WorkspaceMember.id)).where(
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.role == Role.OWNER.value,
                )
            )
        ).scalar_one()
    )


@router.get("", response_model=list[MemberOut])
async def list_members(
    actor: Actor = Depends(require(Permission.OBSERVE)),
    services: AppServices = Depends(get_services),
) -> list[MemberOut]:
    async with services.db.session() as db:
        rows = (
            await db.execute(
                select(WorkspaceMember, User)
                .join(User, User.id == WorkspaceMember.user_id)
                .where(WorkspaceMember.workspace_id == actor.workspace.id)
                .order_by(WorkspaceMember.created_at)
            )
        ).all()
        return [
            MemberOut(
                user_id=user.id,
                email=user.email,
                display_name=user.display_name,
                role=membership.role,
                created_at=membership.created_at,
            )
            for membership, user in rows
        ]


@router.post("", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def add_member(
    body: MemberInvite,
    actor: Actor = Depends(require(Permission.MEMBERS_MANAGE)),
    services: AppServices = Depends(get_services),
) -> MemberOut:
    """Add an existing user to this workspace. Registration stays separate so
    membership changes never create credentials as a side effect."""
    _assert_no_escalation(actor, body.role)
    async with services.db.session() as db:
        user = (
            await db.execute(select(User).where(User.email == body.email.lower()))
        ).scalar_one_or_none()
        if user is None:
            raise NotFoundError(f"no user registered with {body.email}")
        existing = (
            await db.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == actor.workspace.id,
                    WorkspaceMember.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ConflictError("user is already a member of this workspace")
        membership = WorkspaceMember(
            workspace_id=actor.workspace.id, user_id=user.id, role=body.role.value
        )
        db.add(membership)
        await db.flush()
        await record(
            db,
            workspace_id=actor.workspace.id,
            actor_user_id=actor.user.id,
            action=AuditAction.MEMBER_ADDED,
            target_type="user",
            target_id=user.id,
            role=body.role.value,
        )
        return MemberOut(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=membership.role,
            created_at=membership.created_at,
        )


@router.put("/{user_id}", response_model=MemberOut)
async def change_role(
    user_id: str,
    body: MemberRoleUpdate,
    actor: Actor = Depends(require(Permission.MEMBERS_MANAGE)),
    services: AppServices = Depends(get_services),
) -> MemberOut:
    _assert_no_escalation(actor, body.role)
    async with services.db.session() as db:
        membership = (
            await db.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == actor.workspace.id,
                    WorkspaceMember.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            raise NotFoundError("member not found", user_id=user_id)
        if (
            membership.role == Role.OWNER.value
            and body.role is not Role.OWNER
            and await _owner_count(db, actor.workspace.id) == 1
        ):
            raise ConflictError("cannot demote the last owner; promote another owner first")
        previous = membership.role
        membership.role = body.role.value
        user = await db.get(User, user_id)
        assert user is not None
        await record(
            db,
            workspace_id=actor.workspace.id,
            actor_user_id=actor.user.id,
            action=AuditAction.MEMBER_ROLE_CHANGED,
            target_type="user",
            target_id=user_id,
            previous_role=previous,
            new_role=body.role.value,
        )
        return MemberOut(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=membership.role,
            created_at=membership.created_at,
        )


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: str,
    actor: Actor = Depends(require(Permission.MEMBERS_MANAGE)),
    services: AppServices = Depends(get_services),
) -> None:
    async with services.db.session() as db:
        membership = (
            await db.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == actor.workspace.id,
                    WorkspaceMember.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            raise NotFoundError("member not found", user_id=user_id)
        if membership.role == Role.OWNER.value and await _owner_count(db, actor.workspace.id) == 1:
            raise ConflictError("cannot remove the last owner of a workspace")
        await db.delete(membership)
        await record(
            db,
            workspace_id=actor.workspace.id,
            actor_user_id=actor.user.id,
            action=AuditAction.MEMBER_REMOVED,
            target_type="user",
            target_id=user_id,
        )
