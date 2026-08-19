"""Guarded change: apply, verify, and revert.

An agent that can change a host but cannot tell whether the change *worked* is
only half a tool. This package closes that loop.

Every mutation is applied through a guard that records how to undo it, waits for
the system to settle, evaluates the post-conditions the agent declared, and
reverts automatically when they do not hold. Changes that could sever the
operator's own access can additionally carry an expiry: they revert unless a
human confirms, which is the software equivalent of ``reload in 5``.
"""

from hoursx.remediation.conditions import (
    Condition,
    ConditionResult,
    Operator,
    ProbeKind,
    evaluate_conditions,
)
from hoursx.remediation.guard import GuardOutcome, apply_guarded, confirm_change
from hoursx.remediation.ledger import (
    ChangeKind,
    ChangeStatus,
    RevertOutcome,
    record_change,
    revert_change,
    revert_expired_changes,
)

__all__ = [
    "ChangeKind",
    "ChangeStatus",
    "Condition",
    "ConditionResult",
    "GuardOutcome",
    "Operator",
    "ProbeKind",
    "RevertOutcome",
    "apply_guarded",
    "confirm_change",
    "evaluate_conditions",
    "record_change",
    "revert_change",
    "revert_expired_changes",
]
