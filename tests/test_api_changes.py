"""Change ledger over HTTP: the operator's path to undo agent-made changes."""

import pytest

from hoursx.db.models import ChangeRecord


async def _register(api_client, email="owner@example.com") -> dict:
    response = await api_client.post(
        "/v1/auth/register",
        json={"email": email, "password": "long-enough-pass", "display_name": "Owner"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _auth(token: dict) -> dict:
    return {"authorization": f"Bearer {token['access_token']}"}


async def _seed_change(services, workspace_id: str, **overrides) -> str:
    defaults = {
        "workspace_id": workspace_id,
        "run_id": None,
        "kind": "sysctl",
        "target": "vm.swappiness",
        "previous_value": "60",
        "new_value": "10",
        "revertible": True,
        "status": "applied",
        "conditions": [],
    }
    defaults.update(overrides)
    async with services.db.session() as db:
        record = ChangeRecord(**defaults)
        db.add(record)
        await db.flush()
        return record.id


async def test_changes_list_is_empty_initially(api_client):
    token = await _register(api_client)
    assert (await api_client.get("/v1/changes", headers=_auth(token))).json() == []


async def test_changes_list_returns_recorded_changes(api_client, services):
    token = await _register(api_client)
    await _seed_change(services, token["workspace_id"])
    body = (await api_client.get("/v1/changes", headers=_auth(token))).json()
    assert len(body) == 1
    assert body[0]["target"] == "vm.swappiness"
    assert body[0]["previous_value"] == "60"


async def test_changes_are_workspace_isolated(api_client, services):
    owner = await _register(api_client, "a@example.com")
    other = await _register(api_client, "b@example.com")
    await _seed_change(services, owner["workspace_id"])
    assert (await api_client.get("/v1/changes", headers=_auth(other))).json() == []


async def test_changes_can_be_filtered_by_run(api_client, services):
    token = await _register(api_client)
    await _seed_change(services, token["workspace_id"], run_id="run-a")
    await _seed_change(services, token["workspace_id"], run_id="run-b")
    body = (await api_client.get("/v1/changes?run_id=run-a", headers=_auth(token))).json()
    assert len(body) == 1 and body[0]["run_id"] == "run-a"


async def test_revert_of_unknown_change_is_structured_404(api_client):
    token = await _register(api_client)
    response = await api_client.post("/v1/changes/nope/revert", headers=_auth(token))
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_revert_reports_when_system_ops_are_disabled(api_client, services):
    """A disabled deployment must say so rather than silently doing nothing."""
    token = await _register(api_client)
    services.settings.system_ops_enabled = False
    change_id = await _seed_change(services, token["workspace_id"])
    body = (await api_client.post(f"/v1/changes/{change_id}/revert", headers=_auth(token))).json()
    assert body["ok"] is False
    assert "disabled" in body["detail"]


async def test_unrevertible_change_reports_honestly_over_http(api_client, services):
    token = await _register(api_client)
    change_id = await _seed_change(
        services,
        token["workspace_id"],
        kind="service",
        target="nginx",
        new_value="restart",
        revertible=False,
        status="unrevertible",
    )
    body = (await api_client.post(f"/v1/changes/{change_id}/revert", headers=_auth(token))).json()
    assert body["ok"] is False and "no inverse" in body["detail"]


async def test_confirm_clears_the_expiry(api_client, services):
    from datetime import timedelta

    from hoursx.db.models import utcnow

    token = await _register(api_client)
    change_id = await _seed_change(
        services, token["workspace_id"], expires_at=utcnow() + timedelta(seconds=300)
    )
    body = (await api_client.post(f"/v1/changes/{change_id}/confirm", headers=_auth(token))).json()
    assert body["ok"] is True and body["status"] == "confirmed"

    async with services.db.session() as db:
        record = await db.get(ChangeRecord, change_id)
    assert record.expires_at is None


async def test_revert_is_audited(api_client, services):
    token = await _register(api_client)
    change_id = await _seed_change(services, token["workspace_id"])
    await api_client.post(f"/v1/changes/{change_id}/revert", headers=_auth(token))
    events = (await api_client.get("/v1/admin/audit", headers=_auth(token))).json()["events"]
    assert any(event["action"] == "change.reverted" for event in events)


async def test_confirm_is_audited(api_client, services):
    token = await _register(api_client)
    change_id = await _seed_change(services, token["workspace_id"])
    await api_client.post(f"/v1/changes/{change_id}/confirm", headers=_auth(token))
    events = (await api_client.get("/v1/admin/audit", headers=_auth(token))).json()["events"]
    assert any(event["action"] == "change.confirmed" for event in events)


@pytest.mark.parametrize("path", ["/v1/changes"])
async def test_change_endpoints_require_authentication(api_client, path):
    assert (await api_client.get(path)).status_code == 401


async def test_reverting_requires_approval_permission(api_client, services):
    """Viewers can see what changed; only approvers can undo it."""
    owner = await _register(api_client, "owner@example.com")
    viewer = await _register(api_client, "viewer@example.com")
    await api_client.post(
        "/v1/members",
        headers=_auth(owner),
        json={"email": "viewer@example.com", "role": "viewer"},
    )
    change_id = await _seed_change(services, owner["workspace_id"])
    headers = {**_auth(viewer), "x-workspace-id": owner["workspace_id"]}

    assert (await api_client.get("/v1/changes", headers=headers)).status_code == 200
    response = await api_client.post(f"/v1/changes/{change_id}/revert", headers=headers)
    assert response.status_code == 403


async def test_change_tools_are_registered_and_gated(services):
    registry = services.registry
    assert {
        "change.sysctl",
        "change.service",
        "change.list",
        "change.revert",
        "change.confirm",
    } <= set(registry.names())
    for name in ("change.sysctl", "change.service", "change.revert", "change.confirm"):
        assert registry.get(name).spec.requires_approval, f"{name} must be gated"
    assert not registry.get("change.list").spec.requires_approval


async def test_change_tool_schema_exposes_verify_to_the_model():
    """The model can only use verification if the schema advertises it."""
    from hoursx.tools.builtin.change import GuardedSysctlArgs

    schema = GuardedSysctlArgs.model_json_schema()
    assert "verify" in schema["properties"]
    assert "revert" in schema["properties"]["verify"]["description"]
