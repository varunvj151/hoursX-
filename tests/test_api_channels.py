"""Channel ingress: the unauthenticated door external services knock on.

These endpoints have no bearer token to check, so the signature is the whole
trust decision and the status code is a control channel — a ``2xx`` tells the
provider to stop retrying. Both are asserted here, because getting either wrong
loses messages quietly.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from hoursx.api import create_app
from hoursx.channels.base import ChannelKind
from hoursx.db.models import ChannelReply, Run
from test_channels import FakeChannel


@pytest.fixture
def fake(services) -> FakeChannel:
    channel = FakeChannel(secret="knock-knock")
    services.channels.register(channel)
    return channel


@pytest.fixture
async def client(services, fake, seeded):
    """A client whose app has one channel wired up, dispatch running included."""
    app = create_app(services)
    async with (
        httpx.ASGITransport(app=app) as transport,
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as http,
    ):
        yield http


def _signed(body: dict | None = None) -> dict:
    return {"x-fake-secret": "knock-knock", "content-type": "application/json"}


async def _register(client, email="op@b.example.com") -> dict:
    return (await _register_full(client, email))[0]


async def _register_full(client, email="op@b.example.com") -> tuple[dict, str]:
    response = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "long-enough-pass", "display_name": "Op"},
    )
    body = response.json()
    return {"authorization": f"Bearer {body['access_token']}"}, body["workspace_id"]


# --------------------------------------------------------------------- ingress


async def test_webhook_routes_a_message_into_a_run(client, services, seeded, fake):
    response = await client.post(
        "/v1/channels/webhook/webhook",
        headers=_signed(),
        content=json.dumps({"text": "how much disk is left?", "id": "m1"}),
    )
    assert response.status_code == 200

    async with services.db.session() as db:
        from sqlalchemy import select

        replies = (await db.execute(select(ChannelReply))).scalars().all()
        run = await db.get(Run, replies[0].run_id)
    assert len(replies) == 1
    assert run is not None and run.goal == "how much disk is left?"


async def test_webhook_refuses_a_bad_signature(client, services, fake):
    response = await client.post(
        "/v1/channels/webhook/webhook",
        headers={"x-fake-secret": "wrong"},
        content=json.dumps({"text": "let me in"}),
    )
    assert response.status_code == 401
    async with services.db.session() as db:
        from sqlalchemy import select

        assert (await db.execute(select(ChannelReply))).scalars().all() == []


async def test_webhook_acknowledges_traffic_that_is_not_a_message(client, fake):
    """Delivery receipts are ordinary. A non-2xx here would make the provider
    retry forever over something that will never become a message."""
    response = await client.post(
        "/v1/channels/webhook/webhook", headers=_signed(), content=json.dumps({"status": "read"})
    )
    assert response.status_code == 200
    assert "nothing to act on" in response.json()["detail"]


async def test_webhook_for_an_unconfigured_channel_is_not_found(client):
    response = await client.post("/v1/channels/telegram/webhook", content="{}")
    assert response.status_code == 404


async def test_webhook_for_an_unknown_channel_is_not_found(client):
    response = await client.post("/v1/channels/carrier-pigeon/webhook", content="{}")
    assert response.status_code == 404


async def test_webhook_rejects_a_non_json_body(client, fake):
    response = await client.post(
        "/v1/channels/webhook/webhook", headers=_signed(), content=b"<xml/>"
    )
    assert response.status_code == 400


async def test_webhook_rejects_a_json_array(client, fake):
    response = await client.post(
        "/v1/channels/webhook/webhook", headers=_signed(), content=b"[1,2,3]"
    )
    assert response.status_code == 400


async def test_misconfiguration_answers_503_so_the_message_is_retried(client, services, fake):
    """A 200 here would let the provider forget a message nobody ever saw."""
    services.settings.channel_agent_handle = ""
    try:
        response = await client.post(
            "/v1/channels/webhook/webhook",
            headers=_signed(),
            content=json.dumps({"text": "anyone home?"}),
        )
    finally:
        services.settings.channel_agent_handle = "assistant"
    assert response.status_code == 503
    assert "CHANNEL_AGENT_HANDLE" in response.json()["detail"]


async def test_missing_agent_answers_503_rather_than_dropping_the_message(client, services, fake):
    services.settings.channel_agent_handle = "nobody"
    try:
        response = await client.post(
            "/v1/channels/webhook/webhook",
            headers=_signed(),
            content=json.dumps({"text": "hello?"}),
        )
    finally:
        services.settings.channel_agent_handle = "assistant"
    assert response.status_code == 503


async def test_redelivered_webhook_is_acknowledged_without_a_second_run(client, services, fake):
    body = json.dumps({"text": "just once", "id": "m9"})
    first = await client.post("/v1/channels/webhook/webhook", headers=_signed(), content=body)
    second = await client.post("/v1/channels/webhook/webhook", headers=_signed(), content=body)

    assert (first.status_code, second.status_code) == (200, 200)
    assert "accepted 0" in second.json()["detail"]
    async with services.db.session() as db:
        from sqlalchemy import select

        assert len((await db.execute(select(ChannelReply))).scalars().all()) == 1


async def test_the_answer_comes_back_out_the_channel_it_arrived_on(client, services, fake):
    """The whole point, end to end: a message in becomes a message out."""
    await client.post(
        "/v1/channels/webhook/webhook",
        headers=_signed(),
        content=json.dumps({"text": "say something", "id": "e2e"}),
    )
    for _ in range(200):
        if fake.sent:
            break
        await asyncio.sleep(0.02)
    assert fake.sent, "the run finished but nothing was delivered back"
    assert fake.sent[0].conversation_id == "c1"
    assert fake.sent[0].text


# -------------------------------------------------------------------- operator


async def test_status_lists_configured_channels_and_webhook_urls(client, fake):
    headers = await _register(client)
    body = (await client.get("/v1/channels", headers=headers)).json()
    assert body["configured"] == [ChannelKind.WEBHOOK.value]
    assert body["webhook_base"].endswith("/v1/channels")


async def test_status_requires_authentication(client, fake):
    assert (await client.get("/v1/channels")).status_code == 401


async def _own_the_channel(client, services) -> dict:
    """Register an operator and point inbound traffic at their workspace."""
    headers, workspace_id = await _register_full(client)
    agent = await client.post(
        "/v1/agents",
        headers=headers,
        json={"handle": "assistant", "title": "Assistant", "instructions": "Be helpful."},
    )
    assert agent.status_code in (200, 201), agent.text
    async with services.db.session() as db:
        from sqlalchemy import select

        from hoursx.db.models import Workspace

        slug = (
            await db.execute(select(Workspace.slug).where(Workspace.id == workspace_id))
        ).scalar_one()
    services.settings.channel_workspace_slug = slug
    return headers


async def test_bindings_are_listed_after_a_conversation_starts(client, services, seeded, fake):
    headers = await _own_the_channel(client, services)
    await client.post(
        "/v1/channels/webhook/webhook",
        headers=_signed(),
        content=json.dumps({"text": "bind me", "id": "b1"}),
    )
    bindings = (await client.get("/v1/channels/bindings", headers=headers)).json()
    assert len(bindings) == 1
    assert bindings[0]["routing_key"] == "webhook:c1"
    assert bindings[0]["channel"] == ChannelKind.WEBHOOK.value


async def test_delivery_stats_report_what_the_deployment_owes(client, services, seeded, fake):
    headers = await _own_the_channel(client, services)
    before = (await client.get("/v1/channels/deliveries", headers=headers)).json()
    assert before == {"pending": 0, "sent": 0, "abandoned": 0}

    await client.post(
        "/v1/channels/webhook/webhook",
        headers=_signed(),
        content=json.dumps({"text": "count me", "id": "d1"}),
    )
    for _ in range(200):
        stats = (await client.get("/v1/channels/deliveries", headers=headers)).json()
        if stats["sent"]:
            break
        await asyncio.sleep(0.02)
    assert stats == {"pending": 0, "sent": 1, "abandoned": 0}


async def test_bindings_do_not_leak_across_workspaces(client, services, seeded, fake):
    """A binding belongs to the workspace that owns the conversation."""
    await _own_the_channel(client, services)
    await client.post(
        "/v1/channels/webhook/webhook",
        headers=_signed(),
        content=json.dumps({"text": "mine", "id": "x1"}),
    )
    stranger = await _register(client, email="other@b.example.com")
    assert (await client.get("/v1/channels/bindings", headers=stranger)).json() == []


async def test_telegram_registration_needs_telegram_configured(client, fake):
    headers = await _register(client)
    response = await client.post("/v1/channels/telegram/registration", headers=headers)
    assert response.status_code == 500
    assert "not configured" in response.json()["detail"]


async def test_whatsapp_verification_is_not_found_when_unconfigured(client):
    response = await client.get(
        "/v1/channels/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"},
    )
    assert response.status_code == 404
