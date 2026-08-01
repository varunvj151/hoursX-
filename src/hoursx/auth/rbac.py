"""Role-based access control.

Roles are ordered; each maps to a closed permission set. Route handlers declare the
permission they need — they never compare role names directly, so adding a role or
adjusting a grant is a one-file change.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class Permission(StrEnum):
    WORKSPACE_MANAGE = "workspace.manage"
    MEMBERS_MANAGE = "members.manage"
    AGENTS_MANAGE = "agents.manage"
    SESSIONS_USE = "sessions.use"
    RUNS_APPROVE = "runs.approve"
    KNOWLEDGE_WRITE = "knowledge.write"
    KNOWLEDGE_READ = "knowledge.read"
    SCHEDULES_MANAGE = "schedules.manage"
    PLUGINS_MANAGE = "plugins.manage"
    OBSERVE = "observe"


_VIEWER = {Permission.KNOWLEDGE_READ, Permission.OBSERVE}
_MEMBER = _VIEWER | {Permission.SESSIONS_USE, Permission.KNOWLEDGE_WRITE}
_ADMIN = _MEMBER | {
    Permission.AGENTS_MANAGE,
    Permission.RUNS_APPROVE,
    Permission.SCHEDULES_MANAGE,
    Permission.PLUGINS_MANAGE,
    Permission.MEMBERS_MANAGE,
}
_OWNER = _ADMIN | {Permission.WORKSPACE_MANAGE}

_GRANTS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset(_VIEWER),
    Role.MEMBER: frozenset(_MEMBER),
    Role.ADMIN: frozenset(_ADMIN),
    Role.OWNER: frozenset(_OWNER),
}


def role_allows(role: Role | str, permission: Permission) -> bool:
    """True when *role* includes *permission*. Unknown roles grant nothing."""
    try:
        resolved = Role(role)
    except ValueError:
        return False
    return permission in _GRANTS[resolved]
