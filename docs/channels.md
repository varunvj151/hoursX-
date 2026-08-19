# Channels

A **channel** is a two-way transport between an agent and the outside world.
Telegram, WhatsApp, and Gmail are the ones that ship. A person messages a bot,
an agent answers, and the conversation persists — the same session, history, and
memory it would have in the console.

```
Telegram / WhatsApp / Gmail
          │
          ▼
   POST /v1/channels/{kind}/webhook
          │  verify  →  parse  →  route
          ▼
      session  ──►  agent run
          │
          ▼
      dispatcher  ──►  reply back out the same channel
```

The transport is the only provider-specific part. Above the adapter boundary
nothing knows what a Telegram `chat_id` or a Gmail `threadId` is; it sees a
normalised message with a conversation key.

## Setting one up

Every channel needs three things: credentials, a workspace, and an agent.

```bash
HOURSX_CHANNEL_WORKSPACE_SLUG=acme    # which workspace inbound messages belong to
HOURSX_CHANNEL_AGENT_HANDLE=support   # which agent answers them
HOURSX_PUBLIC_URL=https://hoursx.example.com
```

A webhook arrives with no HoursX identity of its own, so those two settings are
how a message finds an owner. If either is missing the ingress answers `503`
rather than `200`: the provider retries, and the operator sees a failure instead
of messages vanishing quietly.

### Telegram

Message **@BotFather** in Telegram and run `/newbot`. It replies with a token.
BotFather is a bot you talk to, not an API you integrate against, so this step
is manual by design.

```bash
HOURSX_TELEGRAM_TOKEN=123456:ABC...
HOURSX_TELEGRAM_WEBHOOK_SECRET=$(openssl rand -hex 32)
```

Then point Telegram at your deployment:

```bash
hoursx channels register
```

Telegram does not sign payloads — it echoes the secret you set here in an
`X-Telegram-Bot-Api-Secret-Token` header. Without a secret, anyone who guesses
your webhook URL can speak to your agent as if they were Telegram. Set one.

### WhatsApp Business Cloud

```bash
HOURSX_WHATSAPP_TOKEN=EAAG...
HOURSX_WHATSAPP_APP_SECRET=...
HOURSX_WHATSAPP_PHONE_NUMBER_ID=123456789
```

In the Meta app dashboard, set the callback URL to
`https://your-host/v1/channels/whatsapp/webhook` and the verify token to your
app secret. The `GET` on that path answers Meta's one-time handshake.

Meta signs each webhook with HMAC-SHA256 over the raw body, which HoursX
verifies before parsing — parsing and re-serialising would change the bytes and
reject authentic payloads.

**The 24-hour window.** WhatsApp only permits free-form replies within 24 hours
of the user's last message. Outside it, only pre-approved templates are allowed.
HoursX surfaces this as an explicit delivery failure (Meta error `131047`)
rather than a silent drop, so an unanswered conversation is visible in
`hoursx channels list`.

### Gmail

```bash
HOURSX_GMAIL_ACCESS_TOKEN=ya29...
HOURSX_GMAIL_ADDRESS=agent@example.com
```

Gmail differs from the chat channels: a Pub/Sub push says *"this mailbox
changed"*, not *"here is a message"*. HoursX stores the last history position
and fetches what changed since. The **first** notification only establishes that
baseline — Gmail enumerates history *after* a position, so there is no window to
read before one exists. One message is missed at setup; the alternative is
guessing.

Replies thread with `threadId`, and messages labelled `SENT`, `DRAFT`, or
`TRASH` are skipped so the agent never answers its own outbound mail.

## What happens to a message

1. **Verify.** The signature or shared secret is checked against the raw bytes.
   A failure is a `401` and a warning — it is a possible forgery, not a bug.
2. **Parse.** Provider payloads become an `InboundMessage`. Delivery receipts,
   typing indicators, and edits are *ignored quietly* and acknowledged `200`;
   they are ordinary traffic, and a non-2xx would make the provider retry them
   forever.
3. **Bind.** The conversation is looked up in `channel_bindings`. A new one
   creates a session; an existing one reuses it, which is what makes the
   conversation continuous.
4. **Route.** A run is submitted with a deduplication key derived from the
   provider's own message id. Every provider retries webhooks it thinks were not
   acknowledged, so a redelivery returns the original run rather than starting a
   second one and doubling the agent's work and spend.
5. **Owe a reply.** A `channel_replies` row is written *now*, not when the run
   finishes. Accepting a message creates an obligation to answer it, and that
   obligation has to outlive the process that took it.
6. **Acknowledge.** The webhook returns. Everything after this is asynchronous.

## Getting the answer back

The dispatcher sweeps owed replies and sends each out the channel it came in on.

**Every terminal run produces a message.** Success sends the answer; failure
sends the reason; cancellation says so. Silence is the one outcome that is never
acceptable — the person on the other end cannot tell it apart from a bot that is
simply broken.

A run parked on human approval gets one interim note ("I need a human decision
before I can continue") and stays owed. When the approval lands, the real answer
follows.

Delivery is **at-least-once**. A dispatcher that dies between sending and
recording the send will try again, so a person may occasionally see an answer
twice. The alternative — marking a reply sent before it is — loses answers, and
a lost answer is indistinguishable from being ignored.

After `HOURSX_CHANNEL_REPLY_MAX_ATTEMPTS` failures (default 5) a reply is marked
`abandoned` with the reason kept. That is a person who asked something and never
heard back, so it is surfaced to operators rather than only logged:

```bash
hoursx channels list
```

```
GET /v1/channels/deliveries   → {"pending": 0, "sent": 128, "abandoned": 1}
```

The API process runs the dispatch loop; the worker also sweeps once a minute as
a safety net for a replica that died holding obligations. A late answer is
recoverable, a lost one is not.

## Operator surface

| Command | Does |
| --- | --- |
| `hoursx channels list` | Configured channels, webhook URLs, live conversations, unsettled replies |
| `hoursx channels register` | Point Telegram at this deployment |
| `hoursx channels dispatch` | Settle owed replies once, without the loop |

| Endpoint | Does |
| --- | --- |
| `GET /v1/channels` | What is configured and where webhooks should point |
| `GET /v1/channels/bindings` | Which external conversations map to which sessions |
| `GET /v1/channels/deliveries` | Pending, sent, and abandoned reply counts |
| `POST /v1/channels/telegram/registration` | Register the webhook (owner only) |
| `POST /v1/channels/{kind}/webhook` | Ingress — unauthenticated, signature-verified |

Webhook registration is an operator action rather than a startup side effect: it
rewrites where Telegram sends *every* update, and a dev machine booting must not
steal production's messages.

## Adding a channel

Implement three methods and the rest is free:

```python
class SlackChannel:
    kind = ChannelKind.SLACK

    def verify(self, *, headers: dict[str, str], body: bytes) -> None:
        """Raise SignatureError if this did not come from Slack."""

    def parse(self, payload: dict) -> InboundMessage | None:
        """Normalise, or None when this is not a message."""

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        """Deliver a reply."""
```

Register it in `build_registry` behind its credential check. Sessions, binding,
deduplication, approval notices, retries, and abandonment are all handled above
the adapter and apply to the new channel unchanged.

Two rules are worth restating because getting them wrong is quiet rather than
loud:

- `verify` must read the **raw bytes**, before any parsing.
- `parse` returns `None` for anything that is not a message. Raising would turn
  a delivery receipt into an error, and providers send a lot of those.
