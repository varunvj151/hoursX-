"""SQLAlchemy models — the single system of record for HoursX.

Conventions: string UUID primary keys (portable across SQLite and PostgreSQL),
UTC timestamps, JSON columns for open-ended payloads, enums stored as strings.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class _Stamped:
    """Mixin: id + created_at shared by every table."""

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


# --------------------------------------------------------------------------- users


class User(_Stamped, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ApiKey(_Stamped, Base):
    __tablename__ = "api_keys"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Workspace(_Stamped, Base):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)


class WorkspaceMember(_Stamped, Base):
    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(24))  # owner | admin | member | viewer


# --------------------------------------------------------------------------- agents


class AgentProfile(_Stamped, Base):
    """A reusable agent definition: model intent, instructions, tool grants."""

    __tablename__ = "agent_profiles"
    __table_args__ = (UniqueConstraint("workspace_id", "handle"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    handle: Mapped[str] = mapped_column(String(64))  # e.g. "researcher"
    title: Mapped[str] = mapped_column(String(120))
    instructions: Mapped[str] = mapped_column(Text, default="")
    model_alias: Mapped[str] = mapped_column(String(64), default="deep")
    tool_grants: Mapped[list] = mapped_column(JSON, default=list)  # tool name globs
    can_delegate: Mapped[bool] = mapped_column(Boolean, default=False)
    max_steps: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Session(_Stamped, Base):
    """A conversation container binding a user, an agent, and a sandbox dir."""

    __tablename__ = "sessions"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    agent_profile_id: Mapped[str] = mapped_column(ForeignKey("agent_profiles.id"))
    title: Mapped[str] = mapped_column(String(200), default="New session")
    sandbox_dir: Mapped[str] = mapped_column(String(400))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    messages: Mapped[list[Message]] = relationship(back_populates="session")


class Message(_Stamped, Base):
    __tablename__ = "messages"

    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | tool | system
    content: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # tool calls/results etc.
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    session: Mapped[Session] = relationship(back_populates="messages")


class Run(_Stamped, Base):
    """One goal-directed execution of an agent within a session."""

    __tablename__ = "runs"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    agent_profile_id: Mapped[str] = mapped_column(ForeignKey("agent_profiles.id"))
    parent_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    # queued | running | awaiting_approval | succeeded | failed | cancelled
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Loop checkpoint: chat transcript needed to resume after an approval pause.
    checkpoint: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Cooperative cancellation: set by the API, observed by the loop between
    # steps. A hard kill mid-tool would leave side effects half-applied.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # Client-supplied dedupe key; a retried submission returns the same run.
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Liveness signal for orphan recovery; NULL once the run is terminal.
    heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)


class RunStep(_Stamped, Base):
    __tablename__ = "run_steps"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    index: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24))  # model | tool | delegation
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)


class ApprovalRequest(_Stamped, Base):
    """A human gate: a tool call suspended until someone decides."""

    __tablename__ = "approval_requests"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(120))
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending | approved | denied
    decided_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)


# --------------------------------------------------------------------------- memory


class MemoryItem(_Stamped, Base):
    """Long-term semantic memory note with its embedding."""

    __tablename__ = "memory_items"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    agent_profile_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(JSON)
    importance: Mapped[float] = mapped_column(Float, default=0.5)


# ------------------------------------------------------------------------ knowledge


class Document(_Stamped, Base):
    __tablename__ = "documents"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(400), default="upload")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|ready|failed
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)


class DocumentChunk(_Stamped, Base):
    __tablename__ = "document_chunks"

    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(JSON)


# ------------------------------------------------------------------------ scheduling


class Schedule(_Stamped, Base):
    """A recurring instruction: fire a goal at an agent on a cron cadence."""

    __tablename__ = "schedules"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    agent_profile_id: Mapped[str] = mapped_column(ForeignKey("agent_profiles.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    cron: Mapped[str] = mapped_column(String(64))  # 5-field cron, UTC
    goal: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(nullable=True)


# --------------------------------------------------------------------------- channels


class ChannelBinding(_Stamped, Base):
    """Ties an external conversation to a HoursX session.

    The binding is what makes a chat with an agent continuous: the same Telegram
    thread reaches the same session, and therefore the same history and memory,
    across restarts and across replicas. Without it every inbound message would
    start a stranger.
    """

    __tablename__ = "channel_bindings"
    __table_args__ = (UniqueConstraint("workspace_id", "routing_key"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    channel: Mapped[str] = mapped_column(String(24))  # telegram | whatsapp | gmail | ...
    # "<channel>:<conversation_id>" — opaque above the adapter layer.
    routing_key: Mapped[str] = mapped_column(String(200), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    sender_id: Mapped[str] = mapped_column(String(120), default="")
    sender_display: Mapped[str] = mapped_column(String(200), default="")


class ChannelCursor(_Stamped, Base):
    """How far a pull-style channel has consumed.

    Gmail notifies that a mailbox changed rather than delivering the message, so
    the change has to be fetched relative to a remembered position. Chat channels
    push the message itself and need no cursor.
    """

    __tablename__ = "channel_cursors"
    __table_args__ = (UniqueConstraint("workspace_id", "channel"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    channel: Mapped[str] = mapped_column(String(24))
    position: Mapped[str] = mapped_column(String(120), default="")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class ChannelReply(_Stamped, Base):
    """A reply owed to an inbound conversation.

    Recorded when the message is accepted, not when the run finishes: accepting
    a message creates an obligation to answer it, and that obligation has to
    outlive the process that took it. The dispatcher settles these rows, so a
    crash between "run finished" and "reply sent" delays the answer instead of
    losing it.

    Unique on ``run_id`` because a redelivered webhook resolves to the same run
    through the idempotency key, and must not owe a second reply.
    """

    __tablename__ = "channel_replies"
    __table_args__ = (UniqueConstraint("run_id"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    channel: Mapped[str] = mapped_column(String(24))
    conversation_id: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(400), default="")
    reply_to: Mapped[str] = mapped_column(String(400), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending | sent | abandoned
    # Claiming bumps this, so it doubles as the compare-and-swap token; there is
    # no separate "sending" state to get stuck in when a dispatcher dies.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # A run parked on human approval gets one interim note so the person is not
    # left in silence; the row stays pending until the real answer exists.
    interim_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)


# --------------------------------------------------------------------------- plugins


class ChangeRecord(_Stamped, Base):
    """A host mutation, with everything needed to undo it.

    Recorded before the outcome is known, so a change is never applied without
    a stored path back. ``previous_value`` is the exact prior state, captured
    at apply time rather than reconstructed later.
    """

    __tablename__ = "change_records"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # sysctl | service
    target: Mapped[str] = mapped_column(String(200))  # sysctl key or unit name
    previous_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str] = mapped_column(Text, default="")
    revertible: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24), default="applied", index=True)
    # applied | verified | reverted | revert_failed | confirmed | unrevertible
    conditions: Mapped[list] = mapped_column(JSON, default=list)
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    # Dead-man switch: revert unless a human confirms before this instant.
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)
    settled_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AuditEvent(_Stamped, Base):
    """Append-only record of consequential actions.

    Written for security-relevant changes (membership, credentials, approvals,
    agent config) — not for ordinary reads, which would drown the signal.
    """

    __tablename__ = "audit_events"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class PluginInstall(_Stamped, Base):
    __tablename__ = "plugin_installs"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(400))
    granted_permissions: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
