"""API surface for keys, members, audit, quota, cancellation, and error shape."""

import asyncio

import pytest


async def _register(api_client, email="owner@example.com", name="Owner") -> dict:
    response = await api_client.post(
        "/v1/auth/register",
        json={"email": email, "password": "long-enough-pass", "display_name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _auth(token: dict) -> dict:
    return {"authorization": f"Bearer {token['access_token']}"}


# ------------------------------------------------------------------ api keys


async def test_api_key_created_once_and_usable(api_client):
    token = await _register(api_client)
    headers = _auth(token)
    created = await api_client.post("/v1/api-keys", headers=headers, json={"name": "ci"})
    assert created.status_code == 201
    secret = created.json()["key"]
    assert secret.startswith("hx_")

    # The key authenticates on its own, with no bearer token present.
    listed = await api_client.get("/v1/agents", headers={"x-api-key": secret})
    assert listed.status_code == 200


async def test_api_key_is_never_disclosed_again(api_client):
    token = await _register(api_client)
    headers = _auth(token)
    await api_client.post("/v1/api-keys", headers=headers, json={"name": "ci"})
    listed = (await api_client.get("/v1/api-keys", headers=headers)).json()
    assert listed and "key" not in listed[0]


async def test_revoked_key_stops_authenticating(api_client):
    token = await _register(api_client)
    headers = _auth(token)
    created = (await api_client.post("/v1/api-keys", headers=headers, json={"name": "temp"})).json()
    assert (
        await api_client.get("/v1/agents", headers={"x-api-key": created["key"]})
    ).status_code == 200

    revoked = await api_client.delete(f"/v1/api-keys/{created['id']}", headers=headers)
    assert revoked.status_code == 204
    assert (
        await api_client.get("/v1/agents", headers={"x-api-key": created["key"]})
    ).status_code == 401


async def test_unknown_api_key_is_rejected(api_client):
    assert (
        await api_client.get("/v1/agents", headers={"x-api-key": "hx_not_a_real_key"})
    ).status_code == 401


async def test_cannot_revoke_another_users_key(api_client):
    owner = await _register(api_client, "a@example.com")
    other = await _register(api_client, "b@example.com")
    created = (
        await api_client.post("/v1/api-keys", headers=_auth(owner), json={"name": "mine"})
    ).json()
    response = await api_client.delete(f"/v1/api-keys/{created['id']}", headers=_auth(other))
    assert response.status_code == 404


# ------------------------------------------------------------------- members


async def test_owner_can_add_and_list_members(api_client):
    owner = await _register(api_client, "owner@example.com")
    await _register(api_client, "teammate@example.com", "Teammate")
    added = await api_client.post(
        "/v1/members",
        headers=_auth(owner),
        json={"email": "teammate@example.com", "role": "member"},
    )
    assert added.status_code == 201
    members = (await api_client.get("/v1/members", headers=_auth(owner))).json()
    assert {m["email"] for m in members} == {"owner@example.com", "teammate@example.com"}


async def test_adding_unknown_user_is_not_found(api_client):
    owner = await _register(api_client)
    response = await api_client.post(
        "/v1/members", headers=_auth(owner), json={"email": "ghost@example.com"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_duplicate_membership_conflicts(api_client):
    owner = await _register(api_client, "owner@example.com")
    await _register(api_client, "dup@example.com")
    body = {"email": "dup@example.com", "role": "member"}
    assert (
        await api_client.post("/v1/members", headers=_auth(owner), json=body)
    ).status_code == 201
    conflict = await api_client.post("/v1/members", headers=_auth(owner), json=body)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "conflict"


async def test_role_can_be_changed(api_client):
    owner = await _register(api_client, "owner@example.com")
    member = await _register(api_client, "m@example.com")
    await api_client.post(
        "/v1/members", headers=_auth(owner), json={"email": "m@example.com", "role": "viewer"}
    )
    updated = await api_client.put(
        f"/v1/members/{member['user_id']}", headers=_auth(owner), json={"role": "admin"}
    )
    assert updated.status_code == 200 and updated.json()["role"] == "admin"


async def test_last_owner_cannot_be_demoted(api_client):
    """A workspace must never become unadministrable."""
    owner = await _register(api_client)
    response = await api_client.put(
        f"/v1/members/{owner['user_id']}", headers=_auth(owner), json={"role": "member"}
    )
    assert response.status_code == 409
    assert "last owner" in response.json()["detail"]


async def test_last_owner_cannot_be_removed(api_client):
    owner = await _register(api_client)
    response = await api_client.delete(f"/v1/members/{owner['user_id']}", headers=_auth(owner))
    assert response.status_code == 409


async def test_admin_cannot_grant_owner(api_client):
    """No privilege escalation by proxy."""
    owner = await _register(api_client, "owner@example.com")
    admin = await _register(api_client, "admin@example.com")
    target = await _register(api_client, "target@example.com")
    await api_client.post(
        "/v1/members",
        headers=_auth(owner),
        json={"email": "admin@example.com", "role": "admin"},
    )
    await api_client.post(
        "/v1/members",
        headers=_auth(owner),
        json={"email": "target@example.com", "role": "member"},
    )
    # The admin's own token resolves to the workspace they were added to.
    escalation = await api_client.put(
        f"/v1/members/{target['user_id']}",
        headers={**_auth(admin), "x-workspace-id": owner["workspace_id"]},
        json={"role": "owner"},
    )
    assert escalation.status_code == 422
    assert escalation.json()["code"] == "validation_failed"


async def test_member_cannot_manage_members(api_client):
    owner = await _register(api_client, "owner@example.com")
    member = await _register(api_client, "member@example.com")
    await api_client.post(
        "/v1/members",
        headers=_auth(owner),
        json={"email": "member@example.com", "role": "member"},
    )
    response = await api_client.post(
        "/v1/members",
        headers={**_auth(member), "x-workspace-id": owner["workspace_id"]},
        json={"email": "owner@example.com", "role": "viewer"},
    )
    assert response.status_code == 403


async def test_member_removal_succeeds_when_another_owner_exists(api_client):
    owner = await _register(api_client, "owner@example.com")
    second = await _register(api_client, "second@example.com")
    await api_client.post(
        "/v1/members",
        headers=_auth(owner),
        json={"email": "second@example.com", "role": "owner"},
    )
    removed = await api_client.delete(f"/v1/members/{second['user_id']}", headers=_auth(owner))
    assert removed.status_code == 204


# --------------------------------------------------------------------- audit


async def test_audit_records_member_and_key_actions(api_client):
    owner = await _register(api_client, "owner@example.com")
    await _register(api_client, "t@example.com")
    await api_client.post(
        "/v1/members", headers=_auth(owner), json={"email": "t@example.com", "role": "member"}
    )
    await api_client.post("/v1/api-keys", headers=_auth(owner), json={"name": "k"})

    events = (await api_client.get("/v1/admin/audit", headers=_auth(owner))).json()["events"]
    actions = {event["action"] for event in events}
    assert "member.added" in actions and "api_key.created" in actions


async def test_audit_requires_member_management_permission(api_client):
    owner = await _register(api_client, "owner@example.com")
    viewer = await _register(api_client, "viewer@example.com")
    await api_client.post(
        "/v1/members",
        headers=_auth(owner),
        json={"email": "viewer@example.com", "role": "viewer"},
    )
    response = await api_client.get(
        "/v1/admin/audit",
        headers={**_auth(viewer), "x-workspace-id": owner["workspace_id"]},
    )
    assert response.status_code == 403


async def test_agent_creation_is_audited(api_client):
    owner = await _register(api_client)
    await api_client.post(
        "/v1/agents", headers=_auth(owner), json={"handle": "auditee", "title": "A"}
    )
    events = (await api_client.get("/v1/admin/audit", headers=_auth(owner))).json()["events"]
    assert any(event["action"] == "agent.created" for event in events)


# --------------------------------------------------------------------- quota


async def test_quota_endpoint_reports_live_usage(api_client):
    owner = await _register(api_client)
    quota = (await api_client.get("/v1/admin/quota", headers=_auth(owner))).json()
    assert quota["active_runs"] == 0
    assert quota["max_concurrent_runs"] >= 1


# -------------------------------------------------------------- cancellation


async def test_cancel_endpoint_settles_a_queued_run(api_client, services):
    owner = await _register(api_client)
    headers = _auth(owner)
    agent = (
        await api_client.post(
            "/v1/agents", headers=headers, json={"handle": "canceller", "title": "C"}
        )
    ).json()
    session = (
        await api_client.post(
            "/v1/sessions", headers=headers, json={"agent_profile_id": agent["id"]}
        )
    ).json()
    run_id = (
        await api_client.post(
            f"/v1/sessions/{session['id']}/messages", headers=headers, json={"text": "go"}
        )
    ).json()["run_id"]

    await api_client.post(f"/v1/runs/{run_id}/cancel", headers=headers)
    for _ in range(60):
        run = (await api_client.get(f"/v1/runs/{run_id}", headers=headers)).json()
        if run["status"] in ("cancelled", "succeeded"):
            break
        await asyncio.sleep(0.03)
    assert run["status"] in ("cancelled", "succeeded")


async def test_cancel_unknown_run_returns_structured_404(api_client):
    owner = await _register(api_client)
    response = await api_client.post("/v1/runs/nonexistent/cancel", headers=_auth(owner))
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# ---------------------------------------------------------------- idempotency


async def test_idempotent_submission_returns_one_run(api_client):
    owner = await _register(api_client)
    headers = _auth(owner)
    agent = (
        await api_client.post("/v1/agents", headers=headers, json={"handle": "idem", "title": "I"})
    ).json()
    session = (
        await api_client.post(
            "/v1/sessions", headers=headers, json={"agent_profile_id": agent["id"]}
        )
    ).json()
    body = {"text": "only once", "idempotency_key": "retry-1"}
    first = await api_client.post(
        f"/v1/sessions/{session['id']}/messages", headers=headers, json=body
    )
    second = await api_client.post(
        f"/v1/sessions/{session['id']}/messages", headers=headers, json=body
    )
    assert first.json()["run_id"] == second.json()["run_id"]


# ------------------------------------------------------------- error contract


async def test_domain_errors_carry_a_stable_code(api_client):
    owner = await _register(api_client)
    response = await api_client.get("/v1/runs/missing-run", headers=_auth(owner))
    body = response.json()
    assert response.status_code == 404
    assert body["code"] == "not_found"
    assert "detail" in body


async def test_duplicate_agent_handle_reports_conflict_code(api_client):
    owner = await _register(api_client)
    headers = _auth(owner)
    body = {"handle": "dupe", "title": "D"}
    assert (await api_client.post("/v1/agents", headers=headers, json=body)).status_code == 201
    conflict = await api_client.post("/v1/agents", headers=headers, json=body)
    assert conflict.status_code == 409 and conflict.json()["code"] == "conflict"


async def test_readyz_reports_per_dependency_status(api_client):
    body = (await api_client.get("/readyz")).json()
    assert body["ok"] is True
    assert body["checks"]["database"] == "ok"


@pytest.mark.parametrize(
    "path",
    ["/v1/api-keys", "/v1/members", "/v1/admin/audit", "/v1/admin/quota"],
)
async def test_new_endpoints_require_authentication(api_client, path):
    assert (await api_client.get(path)).status_code == 401
