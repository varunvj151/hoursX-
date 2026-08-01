"""Workspace quotas and keyset pagination."""

from datetime import datetime, timedelta

import pytest

from hoursx.db.models import Run, utcnow
from hoursx.errors import QuotaExceededError, ValidationError
from hoursx.pagination import Cursor, build_page, resolve_page_size
from hoursx.quotas import QuotaPolicy, current_usage, enforce_run_quota

# ------------------------------------------------------------------- quotas


def _run(seeded, status: str = "running", created_at=None) -> Run:
    run = Run(
        workspace_id=seeded.workspace_id,
        session_id=seeded.session_id,
        agent_profile_id=seeded.profile_id,
        goal="work",
        status=status,
    )
    if created_at is not None:
        run.created_at = created_at
    return run


def test_policy_flags_reflect_zero_as_disabled():
    assert not QuotaPolicy(max_concurrent_runs=0).concurrency_enabled
    assert not QuotaPolicy(max_runs_per_hour=0).rate_enabled
    assert QuotaPolicy(max_concurrent_runs=3).concurrency_enabled


async def test_usage_counts_only_active_runs(services, seeded):
    async with services.db.session() as db:
        db.add(_run(seeded, "running"))
        db.add(_run(seeded, "queued"))
        db.add(_run(seeded, "awaiting_approval"))
        db.add(_run(seeded, "succeeded"))
        db.add(_run(seeded, "failed"))
    async with services.db.session() as db:
        usage = await current_usage(db, workspace_id=seeded.workspace_id)
    assert usage.active_runs == 3
    assert usage.runs_last_hour == 5


async def test_usage_is_workspace_scoped(services, seeded):
    async with services.db.session() as db:
        db.add(_run(seeded, "running"))
    async with services.db.session() as db:
        usage = await current_usage(db, workspace_id="some-other-workspace")
    assert usage.active_runs == 0


async def test_concurrency_quota_blocks_at_the_limit(services, seeded):
    policy = QuotaPolicy(max_concurrent_runs=2, max_runs_per_hour=0)
    async with services.db.session() as db:
        db.add(_run(seeded, "running"))
        db.add(_run(seeded, "running"))
    async with services.db.session() as db:
        with pytest.raises(QuotaExceededError) as caught:
            await enforce_run_quota(db, workspace_id=seeded.workspace_id, policy=policy)
    assert caught.value.context["limit"] == "max_concurrent_runs"
    assert caught.value.status == 429


async def test_concurrency_quota_allows_below_the_limit(services, seeded):
    policy = QuotaPolicy(max_concurrent_runs=5, max_runs_per_hour=0)
    async with services.db.session() as db:
        db.add(_run(seeded, "running"))
    async with services.db.session() as db:
        usage = await enforce_run_quota(db, workspace_id=seeded.workspace_id, policy=policy)
    assert usage.active_runs == 1


async def test_hourly_quota_ignores_older_runs(services, seeded):
    policy = QuotaPolicy(max_concurrent_runs=0, max_runs_per_hour=2)
    stale = utcnow() - timedelta(hours=3)
    async with services.db.session() as db:
        for _ in range(5):
            db.add(_run(seeded, "succeeded", created_at=stale))
    async with services.db.session() as db:
        usage = await enforce_run_quota(db, workspace_id=seeded.workspace_id, policy=policy)
    assert usage.runs_last_hour == 0


async def test_hourly_quota_blocks_a_runaway_loop(services, seeded):
    policy = QuotaPolicy(max_concurrent_runs=0, max_runs_per_hour=3)
    async with services.db.session() as db:
        for _ in range(3):
            db.add(_run(seeded, "succeeded"))
    async with services.db.session() as db:
        with pytest.raises(QuotaExceededError) as caught:
            await enforce_run_quota(db, workspace_id=seeded.workspace_id, policy=policy)
    assert caught.value.context["limit"] == "max_runs_per_hour"


async def test_disabled_quotas_never_block(services, seeded):
    policy = QuotaPolicy(max_concurrent_runs=0, max_runs_per_hour=0)
    async with services.db.session() as db:
        for _ in range(50):
            db.add(_run(seeded, "running"))
    async with services.db.session() as db:
        await enforce_run_quota(db, workspace_id=seeded.workspace_id, policy=policy)


# --------------------------------------------------------------- pagination


def test_cursor_roundtrip_preserves_anchor():
    original = Cursor(created_at=datetime(2026, 8, 1, 12, 30, 45, 123456), id="abc123")
    decoded = Cursor.decode(original.encode())
    assert decoded == original


def test_cursor_encoding_is_url_safe():
    token = Cursor(created_at=datetime(2026, 8, 1), id="x" * 32).encode()
    assert "+" not in token and "/" not in token and "=" not in token


@pytest.mark.parametrize("bad", ["not-base64!!", "", "YWJj", "###"])
def test_malformed_cursor_is_rejected(bad):
    with pytest.raises(ValidationError):
        Cursor.decode(bad)


def test_page_size_defaults_and_clamps():
    assert resolve_page_size(None, default=50, maximum=200) == 50
    assert resolve_page_size(10, default=50, maximum=200) == 10
    assert resolve_page_size(9999, default=50, maximum=200) == 200


def test_page_size_rejects_non_positive():
    with pytest.raises(ValidationError):
        resolve_page_size(0, default=50, maximum=200)


class _Row:
    def __init__(self, id_: str, created_at: datetime) -> None:
        self.id = id_
        self.created_at = created_at


def test_build_page_emits_cursor_only_when_more_remain():
    rows = [_Row(f"id{i}", datetime(2026, 8, 1, 12, 0, i)) for i in range(4)]
    page = build_page(rows, limit=3)
    assert len(page.items) == 3
    assert page.next_cursor is not None

    exact = build_page(rows[:3], limit=3)
    assert exact.next_cursor is None


def test_build_page_handles_empty_results():
    page = build_page([], limit=10)
    assert page.items == [] and page.next_cursor is None


def test_next_cursor_anchors_on_the_last_returned_row():
    rows = [_Row(f"id{i}", datetime(2026, 8, 1, 12, 0, i)) for i in range(3)]
    page = build_page(rows, limit=2)
    anchor = Cursor.decode(page.next_cursor)
    assert anchor.id == "id1"  # the last item actually returned
