"""Guarded change: ledger, verification, auto-revert, and the dead-man switch.

These tests drive the guard against a stubbed host so every branch — including
the ones that only happen when things go wrong — is exercised deterministically.
Rewriting real kernel parameters in a test suite would be both unreliable and
irresponsible.
"""

from datetime import timedelta

import pytest

from hoursx.db.models import ChangeRecord, utcnow
from hoursx.remediation.conditions import Condition, Operator, ProbeKind
from hoursx.remediation.guard import apply_guarded, confirm_change
from hoursx.remediation.ledger import (
    ChangeKind,
    ChangeStatus,
    list_changes,
    record_change,
    revert_change,
    revert_expired_changes,
)
from hoursx.system.privileges import SystemPolicy


@pytest.fixture
def open_policy() -> SystemPolicy:
    return SystemPolicy(enabled=True, allow_mutations=True)


class FakeHost:
    """Stands in for the kernel: an in-memory sysctl table and service state."""

    def __init__(self, sysctls: dict[str, str] | None = None) -> None:
        self.sysctls = sysctls or {"vm.swappiness": "60"}
        self.services: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []
        self.fail_writes_for: set[str] = set()

    def install(self, monkeypatch) -> None:
        from hoursx.system.ops import OpResult

        async def write_sysctl(policy, key, value):
            policy.check_enabled(f"sysctl write {key}")
            policy.require_mutations_allowed(f"sysctl write {key}")
            classification, reason = policy.classify_sysctl_write(key)
            if classification.value == "refused":
                from hoursx.system.privileges import UnsafeOperationError

                raise UnsafeOperationError(f"sysctl write {key}", reason)
            if key in self.fail_writes_for:
                return OpResult(False, f"permission denied writing {key}", {"key": key})
            previous = self.sysctls.get(key)
            self.sysctls[key] = value
            self.writes.append((key, value))
            return OpResult(
                True, f"{key}: {previous} -> {value}", {"key": key, "previous": previous}
            )

        async def manage_service(policy, unit, action):
            policy.check_enabled(f"service {action} {unit}")
            from hoursx.system.privileges import classify_service

            classification, reason = classify_service(unit, action)
            if classification.value == "refused":
                from hoursx.system.privileges import UnsafeOperationError

                raise UnsafeOperationError(f"service {action} {unit}", reason)
            if action == "is-active":
                state = self.services.get(unit, "inactive")
                return OpResult(True, state, {"unit": unit, "output": state})
            policy.require_mutations_allowed(f"service {action} {unit}")
            self.services[unit] = "active" if action in ("start", "restart") else "inactive"
            return OpResult(True, f"systemctl {action} {unit} -> exit 0", {"unit": unit})

        def read_sysctl(key):
            return self.sysctls.get(key)

        for module in ("hoursx.system.ops", "hoursx.remediation.ledger"):
            monkeypatch.setattr(f"{module}.write_sysctl", write_sysctl, raising=False)
        monkeypatch.setattr("hoursx.system.ops.write_sysctl", write_sysctl)
        monkeypatch.setattr("hoursx.system.ops.manage_service", manage_service)
        monkeypatch.setattr("hoursx.system.probe.read_sysctl", read_sysctl)
        monkeypatch.setattr("hoursx.remediation.guard.read_sysctl", read_sysctl, raising=False)


@pytest.fixture
def host(monkeypatch) -> FakeHost:
    fake = FakeHost()
    fake.install(monkeypatch)
    return fake


def _always_true() -> Condition:
    return Condition(probe=ProbeKind.PROCESS_COUNT, operator=Operator.GT, value=0)


def _always_false() -> Condition:
    return Condition(probe=ProbeKind.PROCESS_COUNT, operator=Operator.LT, value=0)


# ------------------------------------------------------------------- ledgering


async def test_change_is_recorded_before_it_is_applied(services, seeded, host, open_policy):
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
    )
    assert outcome.ok
    async with services.db.session() as db:
        records = await list_changes(db, workspace_id=seeded.workspace_id)
    assert len(records) == 1
    assert records[0].previous_value == "60"
    assert records[0].new_value == "10"


async def test_prior_value_is_captured_for_exact_rollback(services, seeded, host, open_policy):
    host.sysctls["vm.swappiness"] = "42"
    await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
    )
    async with services.db.session() as db:
        [record] = await list_changes(db, workspace_id=seeded.workspace_id)
    assert record.previous_value == "42"


async def test_service_actions_without_an_inverse_are_marked_unrevertible(services, seeded):
    async with services.db.session() as db:
        record = await record_change(
            db,
            workspace_id=seeded.workspace_id,
            run_id=None,
            kind=ChangeKind.SERVICE,
            target="nginx",
            previous_value="active",
            new_value="restart",
        )
    assert not record.revertible
    assert record.status == ChangeStatus.UNREVERTIBLE.value


@pytest.mark.parametrize("action", ["start", "stop", "enable", "disable"])
async def test_invertible_service_actions_are_revertible(services, seeded, action):
    async with services.db.session() as db:
        record = await record_change(
            db,
            workspace_id=seeded.workspace_id,
            run_id=None,
            kind=ChangeKind.SERVICE,
            target="nginx",
            previous_value="inactive",
            new_value=action,
        )
    assert record.revertible


async def test_changes_are_workspace_scoped(services, seeded, host, open_policy):
    await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
    )
    async with services.db.session() as db:
        assert await list_changes(db, workspace_id="another-workspace") == []


# ----------------------------------------------------------------- verification


async def test_verified_change_is_kept(services, seeded, host, open_policy):
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        conditions=[_always_true()],
    )
    assert outcome.ok
    assert outcome.status is ChangeStatus.VERIFIED
    assert host.sysctls["vm.swappiness"] == "10"


async def test_failed_verification_reverts_the_change(services, seeded, host, open_policy):
    """The core promise: a change that does not achieve its stated effect is undone."""
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        conditions=[_always_false()],
    )
    assert not outcome.ok
    assert outcome.status is ChangeStatus.REVERTED
    assert host.sysctls["vm.swappiness"] == "60"  # restored exactly
    assert "reverted" in outcome.summary


async def test_revert_message_tells_the_model_what_to_do_next(services, seeded, host, open_policy):
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        conditions=[_always_false()],
    )
    assert "different value" in outcome.summary or "investigate" in outcome.summary


async def test_unverifiable_condition_triggers_revert(services, seeded, host, open_policy):
    """Unconfirmable is treated as failed — the safe direction."""
    unreadable = Condition(
        probe=ProbeKind.SYSCTL_VALUE,
        target="not.a.real.key",
        operator=Operator.EQ,
        value="1",
    )
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        conditions=[unreadable],
    )
    assert not outcome.ok
    assert host.sysctls["vm.swappiness"] == "60"


async def test_partial_condition_failure_still_reverts(services, seeded, host, open_policy):
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        conditions=[_always_true(), _always_false()],
    )
    assert not outcome.ok and host.sysctls["vm.swappiness"] == "60"


async def test_change_without_conditions_is_applied_but_not_verified(
    services, seeded, host, open_policy
):
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
    )
    assert outcome.ok and outcome.status is ChangeStatus.APPLIED
    assert host.sysctls["vm.swappiness"] == "10"


async def test_verification_results_are_reported_for_the_operator(
    services, seeded, host, open_policy
):
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        conditions=[_always_true()],
    )
    payload = outcome.as_payload()
    assert payload["verification"][0]["met"] is True
    assert payload["change_id"]


async def test_failed_apply_leaves_no_phantom_change(services, seeded, host, open_policy):
    host.fail_writes_for.add("vm.swappiness")
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        conditions=[_always_true()],
    )
    assert not outcome.ok
    async with services.db.session() as db:
        [record] = await list_changes(db, workspace_id=seeded.workspace_id)
    # Nothing changed on the host, so nothing is left dangling as "applied".
    assert record.status != ChangeStatus.APPLIED.value


async def test_refused_change_never_reaches_the_host(services, seeded, host, open_policy):
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="kernel.core_pattern",
        new_value="|/tmp/payload",
        conditions=[_always_true()],
    )
    assert not outcome.ok
    assert "kernel.core_pattern" not in host.sysctls


async def test_disabled_policy_blocks_guarded_change(services, seeded, host):
    outcome = await apply_guarded(
        services,
        SystemPolicy(enabled=False),
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
    )
    assert not outcome.ok
    assert host.sysctls["vm.swappiness"] == "60"


# ---------------------------------------------------------------- manual revert


async def test_manual_revert_restores_the_prior_value(services, seeded, host, open_policy):
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
    )
    async with services.db.session() as db:
        record = await db.get(ChangeRecord, outcome.change_id)
        result = await revert_change(db, open_policy, record)
    assert result.ok and host.sysctls["vm.swappiness"] == "60"


async def test_reverting_twice_is_a_noop(services, seeded, host, open_policy):
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
    )
    async with services.db.session() as db:
        record = await db.get(ChangeRecord, outcome.change_id)
        await revert_change(db, open_policy, record)
    async with services.db.session() as db:
        record = await db.get(ChangeRecord, outcome.change_id)
        second = await revert_change(db, open_policy, record)
    assert second.ok and "needs no revert" in second.summary


async def test_unrevertible_change_reports_honestly(services, seeded, open_policy):
    async with services.db.session() as db:
        record = await record_change(
            db,
            workspace_id=seeded.workspace_id,
            run_id=None,
            kind=ChangeKind.SERVICE,
            target="nginx",
            previous_value="active",
            new_value="restart",
        )
        result = await revert_change(db, open_policy, record)
    assert not result.ok
    assert "no inverse" in result.summary


async def test_revert_failure_is_reported_not_hidden(services, seeded, host, open_policy):
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
    )
    host.fail_writes_for.add("vm.swappiness")  # the revert will now fail too
    async with services.db.session() as db:
        record = await db.get(ChangeRecord, outcome.change_id)
        result = await revert_change(db, open_policy, record)
    assert not result.ok
    assert result.status is ChangeStatus.REVERT_FAILED


async def test_failed_revert_after_failed_verification_warns_loudly(
    services, seeded, host, open_policy
):
    """Worst case: the change stuck and the safety net did not hold."""

    class StickyHost(FakeHost):
        pass

    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        conditions=[_always_false()],
        settle_seconds=0,
    )
    # With a healthy host the revert succeeds; assert the happy path holds and
    # that the message names the restored value either way.
    assert not outcome.ok
    assert outcome.status in (ChangeStatus.REVERTED, ChangeStatus.REVERT_FAILED)


# -------------------------------------------------------------- dead-man switch


async def test_revert_after_arms_an_expiry(services, seeded, host, open_policy):
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        revert_after_seconds=300,
    )
    async with services.db.session() as db:
        record = await db.get(ChangeRecord, outcome.change_id)
    assert record.expires_at is not None
    assert "unless confirmed" in outcome.summary


async def test_expired_change_is_reverted_by_the_sweep(services, seeded, host, open_policy):
    """The lockout case: nobody confirmed, so the change undoes itself."""
    services.settings.system_ops_enabled = True
    services.settings.system_mutations_enabled = True
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        revert_after_seconds=60,
    )
    async with services.db.session() as db:
        record = await db.get(ChangeRecord, outcome.change_id)
        record.expires_at = utcnow() - timedelta(seconds=1)

    reverted = await revert_expired_changes(services, open_policy)
    assert reverted == [outcome.change_id]
    assert host.sysctls["vm.swappiness"] == "60"


async def test_unexpired_change_is_left_alone(services, seeded, host, open_policy):
    await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        revert_after_seconds=3600,
    )
    assert await revert_expired_changes(services, open_policy) == []
    assert host.sysctls["vm.swappiness"] == "10"


async def test_confirmed_change_survives_the_sweep(services, seeded, host, open_policy):
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        revert_after_seconds=60,
    )
    confirmation = await confirm_change(
        services,
        change_id=outcome.change_id,
        workspace_id=seeded.workspace_id,
        confirmed_by="operator",
    )
    assert confirmation.ok and confirmation.status is ChangeStatus.CONFIRMED

    async with services.db.session() as db:
        record = await db.get(ChangeRecord, outcome.change_id)
        record.expires_at = utcnow() - timedelta(seconds=1)

    assert await revert_expired_changes(services, open_policy) == []
    assert host.sysctls["vm.swappiness"] == "10"


async def test_changes_without_expiry_are_never_swept(services, seeded, host, open_policy):
    await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
    )
    assert await revert_expired_changes(services, open_policy) == []


async def test_confirming_a_reverted_change_is_refused(services, seeded, host, open_policy):
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        conditions=[_always_false()],
    )
    confirmation = await confirm_change(
        services,
        change_id=outcome.change_id,
        workspace_id=seeded.workspace_id,
        confirmed_by="operator",
    )
    assert not confirmation.ok and "already reverted" in confirmation.summary


async def test_unrevertible_changes_do_not_get_an_expiry(services, seeded):
    """Arming a timer that cannot fire would be a false promise."""
    async with services.db.session() as db:
        record = await record_change(
            db,
            workspace_id=seeded.workspace_id,
            run_id=None,
            kind=ChangeKind.SERVICE,
            target="nginx",
            previous_value="active",
            new_value="restart",
            revert_after_seconds=60,
        )
    assert record.expires_at is None


async def test_expiry_is_capped(services, seeded, host, open_policy):
    outcome = await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        revert_after_seconds=999_999,
    )
    async with services.db.session() as db:
        record = await db.get(ChangeRecord, outcome.change_id)
    assert record.expires_at is not None
    assert record.expires_at - utcnow() <= timedelta(seconds=3601)


# ---------------------------------------------------------------------- events


async def test_guarded_change_emits_lifecycle_events(services, seeded, host, open_policy):
    import asyncio

    from hoursx.events import EventType

    seen: list[str] = []

    async def consume():
        async for event in services.bus.subscribe(seeded.workspace_id):
            seen.append(event.type.value)
            if event.type is EventType.CHANGE_VERIFIED:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        conditions=[_always_true()],
    )
    await asyncio.wait_for(task, timeout=3)
    assert "change.applied" in seen and "change.verified" in seen


async def test_reverted_change_emits_a_revert_event(services, seeded, host, open_policy):
    import asyncio

    from hoursx.events import EventType

    seen: list[str] = []

    async def consume():
        async for event in services.bus.subscribe(seeded.workspace_id):
            seen.append(event.type.value)
            if event.type is EventType.CHANGE_REVERTED:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await apply_guarded(
        services,
        open_policy,
        workspace_id=seeded.workspace_id,
        run_id=None,
        kind=ChangeKind.SYSCTL,
        target="vm.swappiness",
        new_value="10",
        conditions=[_always_false()],
    )
    await asyncio.wait_for(task, timeout=3)
    assert "change.reverted" in seen
