"""Channel adapters, routing, and outbound dispatch.

Adapter tests use recorded provider payloads rather than live calls; the shapes
are the contract, and they are what breaks when a provider changes. Routing and
dispatch tests run against the real database so the claiming and deduplication
rules are exercised as they behave in production, not as a mock repeats them.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json

import pytest

from hoursx.channels.base import (
    ChannelCredentials,
    ChannelKind,
    DeliveryResult,
    InboundMessage,
    OutboundMessage,
    SignatureError,
)
from hoursx.channels.dispatch import ChannelDispatcher
from hoursx.channels.gmail import GmailChannel, _extract_text
from hoursx.channels.router import ChannelRouter, ChannelTarget, build_registry
from hoursx.channels.telegram import TelegramChannel, _split
from hoursx.channels.whatsapp import WhatsAppChannel
from hoursx.config import HoursXSettings
from hoursx.db.models import ChannelBinding, ChannelReply, Run
from hoursx.errors import NotFoundError

# --------------------------------------------------------------------- doubles


class FakeChannel:
    """A channel that records what it was asked to send.

    Registered as ``webhook`` so the fake occupies a real :class:`ChannelKind`
    and travels the same registry, ingress, and dispatch code as a live adapter.
    """

    kind = ChannelKind.WEBHOOK

    def __init__(self, *, secret: str = "", fail_times: int = 0) -> None:
        self.secret = secret
        self.sent: list[OutboundMessage] = []
        self._fail_times = fail_times

    def verify(self, *, headers: dict[str, str], body: bytes) -> None:
        if self.secret and headers.get("x-fake-secret") != self.secret:
            raise SignatureError("fake secret did not match")

    def parse(self, payload):
        if "text" not in payload:
            return None
        return InboundMessage(
            channel=self.kind,
            conversation_id=payload.get("chat", "c1"),
            external_id=payload.get("id", ""),
            sender_id=payload.get("from", "u1"),
            sender_display=payload.get("name", "Someone"),
            text=payload["text"],
        )

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        if self._fail_times > 0:
            self._fail_times -= 1
            return DeliveryResult(False, "transport is down")
        self.sent.append(message)
        return DeliveryResult(True, "delivered")


def _payload(text: str = "hello", **extra) -> dict:
    return {"text": text, **extra}


# ------------------------------------------------------------------- envelope


def test_routing_key_identifies_the_conversation():
    message = InboundMessage(channel=ChannelKind.TELEGRAM, conversation_id="42", sender_id="7")
    assert message.routing_key() == "telegram:42"


def test_dedupe_key_is_stable_across_processes():
    """Salted hashing would make a redelivery on another replica look new."""
    args = {"channel": ChannelKind.TELEGRAM, "conversation_id": "42", "sender_id": "7"}
    first = InboundMessage(**args, external_id="9", text="hi").dedupe_key()
    second = InboundMessage(**args, external_id="9", text="hi").dedupe_key()
    expected = hashlib.sha256(b"telegram:42|9").hexdigest()[:48]
    assert first == second == f"ch:{expected}"


def test_dedupe_key_distinguishes_messages_and_conversations():
    base = {"channel": ChannelKind.TELEGRAM, "sender_id": "7", "text": "hi"}
    same_text_new_id = InboundMessage(**base, conversation_id="42", external_id="10")
    original = InboundMessage(**base, conversation_id="42", external_id="9")
    other_chat = InboundMessage(**base, conversation_id="43", external_id="9")
    assert original.dedupe_key() != same_text_new_id.dedupe_key()
    assert original.dedupe_key() != other_chat.dedupe_key()


def test_dedupe_key_falls_back_to_body_without_a_provider_id():
    base = {"channel": ChannelKind.WEBHOOK, "conversation_id": "c", "sender_id": "u"}
    assert (
        InboundMessage(**base, text="same").dedupe_key()
        == InboundMessage(**base, text="same").dedupe_key()
    )
    assert (
        InboundMessage(**base, text="same").dedupe_key()
        != InboundMessage(**base, text="other").dedupe_key()
    )


def test_dedupe_key_fits_the_idempotency_column():
    key = InboundMessage(
        channel=ChannelKind.GMAIL, conversation_id="t" * 200, sender_id="x", external_id="m" * 200
    ).dedupe_key()
    assert len(key) <= 80


# ------------------------------------------------------------------- telegram


@pytest.fixture
def telegram() -> TelegramChannel:
    return TelegramChannel(ChannelCredentials(token="T", secret="s3cret"))


def test_telegram_accepts_the_configured_secret(telegram):
    telegram.verify(headers={"x-telegram-bot-api-secret-token": "s3cret"}, body=b"{}")


def test_telegram_refuses_a_wrong_secret(telegram):
    with pytest.raises(SignatureError):
        telegram.verify(headers={"x-telegram-bot-api-secret-token": "nope"}, body=b"{}")


def test_telegram_refuses_a_missing_secret_header(telegram):
    """A missing header must fail closed, not fall through as "nothing to check"."""
    with pytest.raises(SignatureError):
        telegram.verify(headers={}, body=b"{}")


def test_telegram_without_a_secret_accepts_anything():
    channel = TelegramChannel(ChannelCredentials(token="T"))
    channel.verify(headers={}, body=b"{}")


def test_telegram_parses_a_message(telegram):
    message = telegram.parse(
        {
            "message": {
                "message_id": 11,
                "chat": {"id": 500},
                "from": {"id": 9, "first_name": "Ada", "last_name": "L"},
                "text": "status?",
            }
        }
    )
    assert message is not None
    assert (message.conversation_id, message.sender_id, message.text) == ("500", "9", "status?")
    assert message.sender_display == "Ada L"
    assert message.external_id == "11"


def test_telegram_parses_an_edit_as_a_message(telegram):
    message = telegram.parse(
        {"edited_message": {"message_id": 2, "chat": {"id": 1}, "from": {"id": 1}, "text": "fix"}}
    )
    assert message is not None and message.text == "fix"


def test_telegram_falls_back_to_username_for_display(telegram):
    message = telegram.parse(
        {"message": {"chat": {"id": 1}, "from": {"id": 2, "username": "ada"}, "text": "hi"}}
    )
    assert message is not None and message.sender_display == "ada"


def test_telegram_reads_a_caption_as_text(telegram):
    message = telegram.parse(
        {
            "message": {
                "chat": {"id": 1},
                "from": {"id": 2},
                "caption": "look at this",
                "photo": [{"file_id": "p"}],
            }
        }
    )
    assert message is not None
    assert message.text == "look at this"
    assert message.attachments == [{"kind": "photo", "payload": [{"file_id": "p"}]}]


@pytest.mark.parametrize(
    "payload",
    [
        {"callback_query": {"id": "1"}},
        {"message": {"chat": {"id": 1}, "from": {"id": 2}}},
        {"message": {"from": {"id": 2}, "text": "orphan"}},
        {"message": "not-a-dict"},
        {},
    ],
    ids=["callback", "no-text", "no-chat", "not-a-dict", "empty"],
)
def test_telegram_ignores_non_messages(telegram, payload):
    """Ignoring must be quiet: these are ordinary webhook traffic, not errors."""
    assert telegram.parse(payload) is None


def test_telegram_split_leaves_short_text_alone():
    assert _split("short", 4096) == ["short"]


def test_telegram_split_never_sends_an_empty_message():
    assert _split("", 4096) == ["(empty response)"]


def test_telegram_split_prefers_line_boundaries():
    text = "\n".join(["x" * 90] * 40)
    chunks = _split(text, 1000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert not any(chunk.startswith("\n") for chunk in chunks)
    assert "".join(chunk.replace("\n", "") for chunk in chunks) == text.replace("\n", "")


def test_telegram_split_handles_text_with_no_breaks():
    chunks = _split("y" * 9000, 4096)
    assert [len(chunk) for chunk in chunks] == [4096, 4096, 808]


# ------------------------------------------------------------------- whatsapp


@pytest.fixture
def whatsapp() -> WhatsAppChannel:
    return WhatsAppChannel(ChannelCredentials(token="T", secret="app-secret", account_id="phone-1"))


def _signature(body: bytes, secret: str = "app-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_whatsapp_accepts_a_valid_signature(whatsapp):
    body = b'{"entry":[]}'
    whatsapp.verify(headers={"x-hub-signature-256": _signature(body)}, body=body)


def test_whatsapp_refuses_a_signature_for_different_bytes(whatsapp):
    """The signature covers the raw body, so re-serialising invalidates it."""
    with pytest.raises(SignatureError):
        whatsapp.verify(headers={"x-hub-signature-256": _signature(b'{"entry": []}')}, body=b"{}")


@pytest.mark.parametrize(
    "header", ["", "sha1=abc", "abc", "sha256="], ids=["missing", "sha1", "bare", "empty-digest"]
)
def test_whatsapp_refuses_malformed_signature_headers(whatsapp, header):
    with pytest.raises(SignatureError):
        whatsapp.verify(headers={"x-hub-signature-256": header} if header else {}, body=b"{}")


def test_whatsapp_parses_a_text_message(whatsapp):
    message = whatsapp.parse(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "contacts": [{"profile": {"name": "Grace"}}],
                                "messages": [
                                    {
                                        "id": "wamid.1",
                                        "from": "15551234567",
                                        "type": "text",
                                        "text": {"body": "is the server up?"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    )
    assert message is not None
    assert message.conversation_id == "15551234567"
    assert message.external_id == "wamid.1"
    assert message.sender_display == "Grace"
    assert message.text == "is the server up?"


def test_whatsapp_ignores_delivery_status_callbacks(whatsapp):
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]}
    assert whatsapp.parse(payload) is None


@pytest.mark.parametrize(
    "payload",
    [{}, {"entry": []}, {"entry": [{"changes": []}]}, {"entry": "bad"}],
    ids=["empty", "no-entries", "no-changes", "wrong-type"],
)
def test_whatsapp_ignores_malformed_envelopes(whatsapp, payload):
    assert whatsapp.parse(payload) is None


def test_whatsapp_answers_the_subscription_handshake(whatsapp):
    challenge = whatsapp.verification_challenge(
        {"hub.mode": "subscribe", "hub.verify_token": "app-secret", "hub.challenge": "1234"}
    )
    assert challenge == "1234"


def test_whatsapp_refuses_a_handshake_with_the_wrong_token(whatsapp):
    assert (
        whatsapp.verification_challenge(
            {"hub.mode": "subscribe", "hub.verify_token": "guess", "hub.challenge": "1234"}
        )
        is None
    )


# ---------------------------------------------------------------------- gmail


@pytest.fixture
def gmail() -> GmailChannel:
    return GmailChannel(ChannelCredentials(token="T", account_id="bot@example.com"))


def _push(notification: dict) -> dict:
    encoded = base64.urlsafe_b64encode(json.dumps(notification).encode()).decode()
    return {"message": {"data": encoded}}


def test_gmail_parses_a_push_notification(gmail):
    message = gmail.parse(_push({"emailAddress": "bot@example.com", "historyId": "9911"}))
    assert message is not None
    assert message.conversation_id == "9911"
    # A notification is a pointer, not a payload: the text is fetched later.
    assert message.text == ""


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": {}},
        {"message": {"data": "!!!not-base64!!!"}},
        {"message": {"data": base64.urlsafe_b64encode(b"not json").decode()}},
        _push({"emailAddress": "bot@example.com"}),
    ],
    ids=["empty", "no-data", "bad-base64", "bad-json", "no-history-id"],
)
def test_gmail_ignores_unusable_notifications(gmail, payload):
    assert gmail.parse(payload) is None


def test_gmail_extracts_text_from_a_nested_mime_tree():
    body = base64.urlsafe_b64encode(b"the actual body").decode()
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/html", "body": {"data": ""}},
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": body}},
                ],
            },
        ],
    }
    assert _extract_text(payload) == "the actual body"


def test_gmail_extract_text_returns_empty_when_absent():
    assert _extract_text({"mimeType": "image/png", "body": {}}) == ""


# ------------------------------------------------------------------- registry


def test_registry_is_empty_without_credentials():
    assert len(build_registry(HoursXSettings(environment="test"))) == 0


def test_registry_builds_each_configured_channel():
    registry = build_registry(
        HoursXSettings(
            environment="test",
            telegram_token="T",
            whatsapp_token="W",
            whatsapp_phone_number_id="p1",
            gmail_access_token="G",
        )
    )
    assert registry.kinds() == ["gmail", "telegram", "whatsapp"]


def test_whatsapp_needs_both_token_and_phone_number():
    """Half-configured is absent, not present and failing on first send."""
    registry = build_registry(HoursXSettings(environment="test", whatsapp_token="W"))
    assert registry.get(ChannelKind.WHATSAPP) is None


# -------------------------------------------------------------------- routing


@pytest.fixture
def fake(services) -> FakeChannel:
    channel = FakeChannel()
    services.channels.register(channel)
    return channel


@pytest.fixture
def target(seeded) -> ChannelTarget:
    return ChannelTarget(workspace_id=seeded.workspace_id, agent_handle="assistant")


async def test_routing_creates_a_session_and_binds_the_conversation(
    services, conductor, seeded, fake, target
):
    channel_router = ChannelRouter(services, conductor)
    routed = await channel_router.handle_inbound(fake.parse(_payload("ping")), target=target)

    assert routed.duplicate is False
    async with services.db.session() as db:
        binding = (await db.execute(_bindings())).scalars().one()
        run = await db.get(Run, routed.run_id)
    assert binding.routing_key == "webhook:c1"
    assert binding.session_id == routed.session_id
    assert run is not None and run.goal == "ping"
    # The conversation is attributed to the workspace's founding member, so it
    # lands inside the same quota and audit trail as console work.
    assert run.workspace_id == seeded.workspace_id


async def test_second_message_reuses_the_same_session(services, conductor, seeded, fake, target):
    channel_router = ChannelRouter(services, conductor)
    first = await channel_router.handle_inbound(fake.parse(_payload("one", id="m1")), target=target)
    second = await channel_router.handle_inbound(
        fake.parse(_payload("two", id="m2")), target=target
    )

    assert first.session_id == second.session_id
    assert first.run_id != second.run_id
    async with services.db.session() as db:
        assert len((await db.execute(_bindings())).scalars().all()) == 1


async def test_redelivered_message_does_not_start_a_second_run(
    services, conductor, seeded, fake, target
):
    """Providers retry; a retry must not double the agent's work or spend."""
    channel_router = ChannelRouter(services, conductor)
    message = fake.parse(_payload("only once", id="m1"))
    first = await channel_router.handle_inbound(message, target=target)
    again = await channel_router.handle_inbound(message, target=target)

    assert again.run_id == first.run_id
    assert again.duplicate is True
    async with services.db.session() as db:
        replies = (await db.execute(_replies())).scalars().all()
    assert len(replies) == 1


async def test_repeated_text_with_a_new_id_is_a_new_run(services, conductor, seeded, fake, target):
    """Someone genuinely sending "ok" twice deserves two answers."""
    channel_router = ChannelRouter(services, conductor)
    first = await channel_router.handle_inbound(fake.parse(_payload("ok", id="m1")), target=target)
    second = await channel_router.handle_inbound(fake.parse(_payload("ok", id="m2")), target=target)
    assert first.run_id != second.run_id


async def test_routing_records_the_reply_obligation(services, conductor, seeded, fake, target):
    channel_router = ChannelRouter(services, conductor)
    routed = await channel_router.handle_inbound(fake.parse(_payload()), target=target)
    async with services.db.session() as db:
        reply = (await db.execute(_replies())).scalars().one()
    assert reply.run_id == routed.run_id
    assert reply.status == "pending"
    assert reply.conversation_id == "c1"


async def test_routing_refuses_when_the_agent_does_not_exist(services, conductor, seeded, fake):
    channel_router = ChannelRouter(services, conductor)
    missing = ChannelTarget(workspace_id=seeded.workspace_id, agent_handle="ghost")
    with pytest.raises(NotFoundError, match="ghost"):
        await channel_router.handle_inbound(fake.parse(_payload()), target=missing)


async def test_target_resolution_reports_a_missing_workspace_slug(services, conductor, settings):
    channel_router = ChannelRouter(services, conductor)
    with pytest.raises(NotFoundError, match="CHANNEL_WORKSPACE_SLUG"):
        await channel_router.resolve_target(
            settings.model_copy(update={"channel_workspace_slug": ""})
        )


async def test_target_resolution_reports_an_unknown_workspace(services, conductor, settings):
    channel_router = ChannelRouter(services, conductor)
    with pytest.raises(NotFoundError, match="no workspace with slug"):
        await channel_router.resolve_target(
            settings.model_copy(update={"channel_workspace_slug": "nope"})
        )


async def test_target_resolution_reports_a_missing_agent_handle(
    services, conductor, seeded, settings
):
    channel_router = ChannelRouter(services, conductor)
    with pytest.raises(NotFoundError, match="CHANNEL_AGENT_HANDLE"):
        await channel_router.resolve_target(
            settings.model_copy(update={"channel_agent_handle": ""})
        )


async def test_target_resolution_succeeds_when_configured(services, conductor, seeded, settings):
    resolved = await ChannelRouter(services, conductor).resolve_target(settings)
    assert resolved == ChannelTarget(workspace_id=seeded.workspace_id, agent_handle="assistant")


def _bindings():
    from sqlalchemy import select

    return select(ChannelBinding)


def _replies():
    from sqlalchemy import select

    return select(ChannelReply)


# ------------------------------------------------------------------- dispatch


async def _owe(services, seeded, *, status: str, answer: str = "", error: str = "") -> str:
    """Create a finished run with a reply owed on it."""
    async with services.db.session() as db:
        run = Run(
            workspace_id=seeded.workspace_id,
            session_id=seeded.session_id,
            agent_profile_id=seeded.profile_id,
            goal="g",
            status=status,
            final_answer=answer or None,
            error=error or None,
        )
        db.add(run)
        await db.flush()
        db.add(
            ChannelReply(
                workspace_id=seeded.workspace_id,
                run_id=run.id,
                channel=ChannelKind.WEBHOOK.value,
                conversation_id="c1",
            )
        )
        return run.id


async def _reply_row(services, run_id):
    from sqlalchemy import select

    async with services.db.session() as db:
        return (
            await db.execute(select(ChannelReply).where(ChannelReply.run_id == run_id))
        ).scalar_one()


async def test_dispatch_sends_a_successful_answer(services, seeded, fake):
    run_id = await _owe(services, seeded, status="succeeded", answer="all healthy")
    sent = await ChannelDispatcher(services, services.channels).sweep_once()

    assert sent == 1
    assert [message.text for message in fake.sent] == ["all healthy"]
    row = await _reply_row(services, run_id)
    assert row.status == "sent" and row.sent_at is not None


async def test_dispatch_reports_a_failure_rather_than_going_silent(services, seeded, fake):
    """Silence is indistinguishable from a broken bot, so failures speak."""
    await _owe(services, seeded, status="failed", error="the tool exploded")
    await ChannelDispatcher(services, services.channels).sweep_once()
    assert "the tool exploded" in fake.sent[0].text


async def test_dispatch_reports_a_cancellation(services, seeded, fake):
    await _owe(services, seeded, status="cancelled")
    await ChannelDispatcher(services, services.channels).sweep_once()
    assert "cancelled" in fake.sent[0].text


async def test_dispatch_speaks_even_when_a_run_produced_no_answer(services, seeded, fake):
    await _owe(services, seeded, status="succeeded")
    await ChannelDispatcher(services, services.channels).sweep_once()
    assert fake.sent[0].text


async def test_dispatch_ignores_runs_that_are_still_working(services, seeded, fake):
    await _owe(services, seeded, status="running")
    assert await ChannelDispatcher(services, services.channels).sweep_once() == 0
    assert fake.sent == []


async def test_dispatch_sends_one_interim_note_while_awaiting_approval(services, seeded, fake):
    run_id = await _owe(services, seeded, status="awaiting_approval")
    dispatcher = ChannelDispatcher(services, services.channels)

    assert await dispatcher.sweep_once() == 1
    assert await dispatcher.sweep_once() == 0  # the note is sent once, not per sweep
    assert len(fake.sent) == 1
    assert "human decision" in fake.sent[0].text

    row = await _reply_row(services, run_id)
    # Still owed: the real answer has not been produced yet.
    assert row.status == "pending" and row.interim_sent is True
    # The note must not consume the delivery budget for the real answer.
    assert row.attempts == 0


async def test_the_real_answer_follows_the_interim_note(services, seeded, fake):
    run_id = await _owe(services, seeded, status="awaiting_approval")
    dispatcher = ChannelDispatcher(services, services.channels)
    await dispatcher.sweep_once()

    async with services.db.session() as db:
        run = await db.get(Run, run_id)
        run.status = "succeeded"
        run.final_answer = "approved and done"

    assert await dispatcher.sweep_once() == 1
    assert [message.text for message in fake.sent][-1] == "approved and done"
    assert (await _reply_row(services, run_id)).status == "sent"


async def test_dispatch_retries_a_failed_send(services, seeded):
    channel = FakeChannel(fail_times=1)
    services.channels.register(channel)
    run_id = await _owe(services, seeded, status="succeeded", answer="eventually")
    dispatcher = ChannelDispatcher(services, services.channels)

    assert await dispatcher.sweep_once() == 0
    assert (await _reply_row(services, run_id)).status == "pending"
    assert await dispatcher.sweep_once() == 1
    assert (await _reply_row(services, run_id)).status == "sent"


async def test_dispatch_gives_up_visibly_after_the_attempt_budget(services, seeded):
    channel = FakeChannel(fail_times=99)
    services.channels.register(channel)
    run_id = await _owe(services, seeded, status="succeeded", answer="never lands")
    dispatcher = ChannelDispatcher(services, services.channels)

    for _ in range(services.settings.channel_reply_max_attempts + 2):
        await dispatcher.sweep_once()

    row = await _reply_row(services, run_id)
    assert row.status == "abandoned"
    assert row.attempts == services.settings.channel_reply_max_attempts
    # An abandoned reply is a person who never heard back; the reason is kept.
    assert "gave up" in row.detail


async def test_dispatch_abandons_a_reply_whose_channel_was_removed(services, seeded):
    run_id = await _owe(services, seeded, status="succeeded", answer="orphaned")
    assert await ChannelDispatcher(services, services.channels).sweep_once() == 0
    row = await _reply_row(services, run_id)
    assert row.status == "abandoned" and "no longer configured" in row.detail


async def test_concurrent_dispatchers_send_a_reply_once(services, seeded, fake):
    """Two replicas sweeping the same row must not double-message the person."""
    await _owe(services, seeded, status="succeeded", answer="exactly once")
    results = await asyncio.gather(
        ChannelDispatcher(services, services.channels).sweep_once(),
        ChannelDispatcher(services, services.channels).sweep_once(),
    )
    assert sum(results) == 1
    assert len(fake.sent) == 1


async def test_dispatch_survives_a_reply_whose_run_vanished(services, seeded, fake):
    run_id = await _owe(services, seeded, status="succeeded", answer="gone")
    async with services.db.session() as db:
        await db.delete(await db.get(Run, run_id))

    assert await ChannelDispatcher(services, services.channels).sweep_once() == 0
    row = await _reply_row(services, run_id)
    assert row.status == "abandoned"


async def test_dispatch_loop_stops_cleanly_when_cancelled(services, seeded, fake):
    await _owe(services, seeded, status="succeeded", answer="from the loop")
    dispatcher = ChannelDispatcher(services, services.channels)
    task = asyncio.create_task(dispatcher.run_forever(interval=0.01))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if fake.sent:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [message.text for message in fake.sent] == ["from the loop"]


async def test_shutdown_never_leaves_a_reply_claimed_but_unsettled(services, seeded, fake):
    """Cancelling the loop must land between sweeps, not inside one.

    A cancel delivered mid-sweep would abandon a claimed delivery with its
    outcome unrecorded and tear down the database session mid-transaction,
    which wedges the connection for everything that runs afterwards.
    """
    run_id = await _owe(services, seeded, status="succeeded", answer="mid-flight")
    task = asyncio.create_task(
        ChannelDispatcher(services, services.channels).run_forever(interval=0.001)
    )
    # Cancel at a moment likely to fall inside a sweep rather than the sleep.
    await asyncio.sleep(0.002)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await _reply_row(services, run_id)
    assert row.status in ("pending", "sent")
    if row.status == "sent":
        assert row.sent_at is not None
    # The session must still work; a torn-down connection hangs here instead.
    assert await ChannelDispatcher(services, services.channels).sweep_once() in (0, 1)
    assert (await _reply_row(services, run_id)).status == "sent"
