"""API request/response models (the public wire contract)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field

# ---------------------------------------------------------------------- auth


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    workspace_id: str


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str


# --------------------------------------------------------------------- agents


class AgentProfileIn(BaseModel):
    handle: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    title: str = Field(min_length=1, max_length=120)
    instructions: str = ""
    model_alias: str = "deep"
    tool_grants: list[str] = Field(default_factory=lambda: ["fs.*", "knowledge.*", "memory.*"])
    can_delegate: bool = False
    max_steps: int | None = Field(default=None, ge=1, le=200)


class AgentProfileOut(AgentProfileIn):
    id: str
    created_at: datetime


# -------------------------------------------------------------------- sessions


class SessionCreate(BaseModel):
    agent_profile_id: str
    title: str = "New session"


class SessionOut(BaseModel):
    id: str
    title: str
    agent_profile_id: str
    created_at: datetime
    archived: bool


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    plan_first: bool = False
    # Optional client-supplied dedupe key; a retry with the same key returns
    # the original run instead of starting a second one.
    idempotency_key: str | None = Field(default=None, max_length=80)


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    run_id: str | None
    created_at: datetime


class SubmitResponse(BaseModel):
    run_id: str


# ----------------------------------------------------------------------- runs


class RunOut(BaseModel):
    id: str
    session_id: str
    status: str
    goal: str
    final_answer: str | None
    error: str | None
    step_count: int
    created_at: datetime
    finished_at: datetime | None
    cancel_requested: bool = False
    input_tokens: int = 0
    output_tokens: int = 0


class RunStepOut(BaseModel):
    index: int
    kind: str
    detail: dict[str, Any]
    created_at: datetime
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class ApprovalOut(BaseModel):
    id: str
    run_id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str
    status: str
    created_at: datetime


class ApprovalDecision(BaseModel):
    approved: bool


# ------------------------------------------------------------------ knowledge


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1)
    source: str = "upload"


class DocumentOut(BaseModel):
    id: str
    title: str
    source: str
    status: str
    chunk_count: int
    created_at: datetime


class SearchHit(BaseModel):
    document_id: str
    document_title: str
    text: str
    score: float


# ------------------------------------------------------------------ schedules


class ScheduleIn(BaseModel):
    agent_profile_id: str
    cron: str = Field(description="5-field cron expression, UTC")
    goal: str = Field(min_length=3)
    enabled: bool = True


class ScheduleOut(ScheduleIn):
    id: str
    last_fired_at: datetime | None
    created_at: datetime


# -------------------------------------------------------------------- plugins


class PluginOut(BaseModel):
    name: str
    version: str
    summary: str
    tools: list[str]
    source: str = "local"
