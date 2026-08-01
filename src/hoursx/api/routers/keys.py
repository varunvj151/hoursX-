"""API key lifecycle.

Keys are machine credentials scoped to the creating user's workspace access.
The plaintext key is returned exactly once, at creation — only its hash is
stored, so a lost key is replaced, never recovered.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from hoursx.api.deps import Actor, get_services, require
from hoursx.audit import AuditAction, record
from hoursx.auth import Permission, hash_api_key, new_api_key
from hoursx.db.models import ApiKey
from hoursx.errors import NotFoundError
from hoursx.services import AppServices

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ApiKeyOut(BaseModel):
    id: str
    name: str
    revoked: bool
    created_at: datetime


class ApiKeyCreated(ApiKeyOut):
    """Creation response — the only time ``key`` is ever disclosed."""

    key: str


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(
    actor: Actor = Depends(require(Permission.OBSERVE)),
    services: AppServices = Depends(get_services),
) -> list[ApiKeyOut]:
    async with services.db.session() as db:
        rows = (
            (
                await db.execute(
                    select(ApiKey)
                    .where(ApiKey.user_id == actor.user.id)
                    .order_by(ApiKey.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [
            ApiKeyOut(id=r.id, name=r.name, revoked=r.revoked, created_at=r.created_at)
            for r in rows
        ]


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: ApiKeyCreate,
    actor: Actor = Depends(require(Permission.SESSIONS_USE)),
    services: AppServices = Depends(get_services),
) -> ApiKeyCreated:
    secret = new_api_key()
    async with services.db.session() as db:
        record_row = ApiKey(user_id=actor.user.id, name=body.name, key_hash=hash_api_key(secret))
        db.add(record_row)
        await db.flush()
        await record(
            db,
            workspace_id=actor.workspace.id,
            actor_user_id=actor.user.id,
            action=AuditAction.API_KEY_CREATED,
            target_type="api_key",
            target_id=record_row.id,
            name=body.name,
        )
        return ApiKeyCreated(
            id=record_row.id,
            name=record_row.name,
            revoked=record_row.revoked,
            created_at=record_row.created_at,
            key=secret,
        )


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    key_id: str,
    actor: Actor = Depends(require(Permission.SESSIONS_USE)),
    services: AppServices = Depends(get_services),
) -> None:
    """Revoke rather than delete: the audit trail must still reference the id."""
    async with services.db.session() as db:
        row = await db.get(ApiKey, key_id)
        if row is None or row.user_id != actor.user.id:
            raise NotFoundError("api key not found", key_id=key_id)
        row.revoked = True
        await record(
            db,
            workspace_id=actor.workspace.id,
            actor_user_id=actor.user.id,
            action=AuditAction.API_KEY_REVOKED,
            target_type="api_key",
            target_id=key_id,
        )
