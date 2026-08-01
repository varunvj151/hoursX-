"""API integration: auth flow, agents, sessions, runs, knowledge, RBAC edges."""

import asyncio

import pytest


async def _register(api_client, email="a@b.example.com") -> dict:
    response = await api_client.post(
        "/v1/auth/register",
        json={"email": email, "password": "long-enough-pass", "display_name": "Ada"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _auth(token: dict) -> dict:
    return {"authorization": f"Bearer {token['access_token']}"}


async def _wait_run(api_client, headers, run_id, *, statuses=("succeeded",)) -> dict:
    for _ in range(100):
        run = (await api_client.get(f"/v1/runs/{run_id}", headers=headers)).json()
        if run["status"] in statuses:
            return run
        await asyncio.sleep(0.03)
    pytest.fail(f"run never reached {statuses}: {run}")


async def test_health_endpoints(api_client):
    assert (await api_client.get("/healthz")).json()["ok"] is True
    assert (await api_client.get("/readyz")).json()["ok"] is True


async def test_register_login_me_flow(api_client):
    token = await _register(api_client)
    me = await api_client.get("/v1/auth/me", headers=_auth(token))
    assert me.json()["email"] == "a@b.example.com"

    login = await api_client.post(
        "/v1/auth/login", json={"email": "a@b.example.com", "password": "long-enough-pass"}
    )
    assert login.status_code == 200
    assert login.json()["workspace_id"] == token["workspace_id"]


async def test_duplicate_registration_conflicts(api_client):
    await _register(api_client)
    response = await api_client.post(
        "/v1/auth/register",
        json={"email": "a@b.example.com", "password": "long-enough-pass", "display_name": "Dup"},
    )
    assert response.status_code == 409


async def test_login_never_reveals_which_field_failed(api_client):
    await _register(api_client)
    wrong_pass = await api_client.post(
        "/v1/auth/login", json={"email": "a@b.example.com", "password": "wrong-password"}
    )
    no_user = await api_client.post(
        "/v1/auth/login", json={"email": "ghost@b.example.com", "password": "wrong-password"}
    )
    assert wrong_pass.status_code == no_user.status_code == 401
    assert wrong_pass.json() == no_user.json()


async def test_unauthenticated_requests_rejected(api_client):
    assert (await api_client.get("/v1/agents")).status_code == 401
    assert (
        await api_client.get("/v1/agents", headers={"authorization": "Bearer junk"})
    ).status_code == 401


async def test_full_chat_flow_via_api(api_client):
    token = await _register(api_client)
    headers = _auth(token)

    agent = await api_client.post(
        "/v1/agents",
        headers=headers,
        json={"handle": "helper", "title": "Helper", "model_alias": "deep"},
    )
    assert agent.status_code == 201, agent.text
    agent_id = agent.json()["id"]

    session = await api_client.post(
        "/v1/sessions",
        headers=headers,
        json={"agent_profile_id": agent_id, "title": "First chat"},
    )
    assert session.status_code == 201
    session_id = session.json()["id"]

    submitted = await api_client.post(
        f"/v1/sessions/{session_id}/messages", headers=headers, json={"text": "hi"}
    )
    assert submitted.status_code == 202
    run = await _wait_run(api_client, headers, submitted.json()["run_id"])
    assert run["final_answer"] == "echo: hi"

    messages = (await api_client.get(f"/v1/sessions/{session_id}/messages", headers=headers)).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]

    steps = (await api_client.get(f"/v1/runs/{run['id']}/steps", headers=headers)).json()
    assert steps and steps[0]["kind"] == "model"


async def test_sse_stream_replays_terminal_state(api_client):
    token = await _register(api_client)
    headers = _auth(token)
    agent = (
        await api_client.post("/v1/agents", headers=headers, json={"handle": "hx", "title": "H"})
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
    await _wait_run(api_client, headers, run_id)

    async with api_client.stream("GET", f"/v1/runs/{run_id}/stream", headers=headers) as response:
        body = ""
        async for chunk in response.aiter_text():
            body += chunk
    assert "event: run.finished" in body and "echo: go" in body


async def test_workspace_isolation_between_users(api_client):
    token_a = await _register(api_client, "a@b.example.com")
    token_b = await _register(api_client, "b@b.example.com")
    agent = (
        await api_client.post(
            "/v1/agents", headers=_auth(token_a), json={"handle": "priv", "title": "P"}
        )
    ).json()
    session = (
        await api_client.post(
            "/v1/sessions", headers=_auth(token_a), json={"agent_profile_id": agent["id"]}
        )
    ).json()
    # User B must not see A's agents or session.
    assert (await api_client.get("/v1/agents", headers=_auth(token_b))).json() == []
    forbidden = await api_client.get(
        f"/v1/sessions/{session['id']}/messages", headers=_auth(token_b)
    )
    assert forbidden.status_code == 404


async def test_knowledge_upload_and_search_via_api(api_client):
    token = await _register(api_client)
    headers = _auth(token)
    upload = await api_client.post(
        "/v1/knowledge/documents",
        headers=headers,
        json={
            "title": "Onboarding",
            "text": "New engineers request access through the identity portal first.",
        },
    )
    assert upload.status_code == 202
    document_id = upload.json()["id"]

    for _ in range(100):
        docs = (await api_client.get("/v1/knowledge/documents", headers=headers)).json()
        if docs and docs[0]["status"] == "ready":
            break
        await asyncio.sleep(0.03)
    else:
        pytest.fail("document never became ready")

    hits = (
        await api_client.get(
            "/v1/knowledge/search",
            headers=headers,
            params={"q": "how do engineers request access"},
        )
    ).json()
    assert hits and hits[0]["document_id"] == document_id


async def test_schedule_crud_and_validation(api_client):
    token = await _register(api_client)
    headers = _auth(token)
    agent = (
        await api_client.post(
            "/v1/agents", headers=headers, json={"handle": "cron", "title": "Cron"}
        )
    ).json()
    bad = await api_client.post(
        "/v1/schedules",
        headers=headers,
        json={"agent_profile_id": agent["id"], "cron": "not a cron", "goal": "daily report"},
    )
    assert bad.status_code == 422
    good = await api_client.post(
        "/v1/schedules",
        headers=headers,
        json={"agent_profile_id": agent["id"], "cron": "0 9 * * *", "goal": "daily report"},
    )
    assert good.status_code == 201
    listed = (await api_client.get("/v1/schedules", headers=headers)).json()
    assert len(listed) == 1
    deleted = await api_client.delete(f"/v1/schedules/{good.json()['id']}", headers=headers)
    assert deleted.status_code == 204


async def test_admin_tools_lists_builtins(api_client):
    token = await _register(api_client)
    tools = (await api_client.get("/v1/admin/tools", headers=_auth(token))).json()["tools"]
    assert "fs.read" in tools and "shell.run" in tools and "agent.delegate" in tools
