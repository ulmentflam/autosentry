# Phase 4.6 — Discord integration

Adds Discord as a first-class destination alongside Slack. Mirrors the
existing Slack dispatcher pattern: outbound notifier writes JSON lines
to a file outbox, dispatcher daemon drains it. Inbound (bot backend
only) polls thread replies and applies the same command grammar that
Slack uses (`abort`, `pause`, `resume`, `set …`, `approve`,
`comment: …`).

## Why Discord

Different communities live in different chat tools. Most ML infra
teams I've talked to are on Slack; OSS and gaming-adjacent ones are on
Discord. autosentry should be neutral — the dispatcher's abstraction
already has room for it.

## Design

### Two backends (parallel to Slack)

- **`DiscordWebhookBackend`** — POST to a Discord incoming webhook
  URL. Outbound-only. Discord webhooks accept a `thread_id` query
  param so they can post into an existing thread; we plumb
  `thread_ts` (which we treat as the Discord thread channel id) into
  that.
- **`DiscordBotBackend`** — Discord HTTP API v10 with a bot token.
  Bidirectional. Posts to channels; opens a thread under the parent
  message on first message under a `thread_key`; polls
  `/channels/{thread.id}/messages?after=<last_msg_id>` for inbound.

### Channel + thread modeling

Discord's thread model differs from Slack's:

- In Slack, threads are identified by a parent message's `ts` and you
  post into them by setting `thread_ts`.
- In Discord, threads are distinct channels with their own IDs. You
  post to a thread by using the thread channel ID as the channel.

We keep the existing `deliver(channel, text, thread_ts)` interface
and let the Discord backends interpret `thread_ts` as the Discord
thread channel id. The dispatcher's per-thread_key state mapping
still works — first message under a thread_key gets a thread channel
id, we remember it, later messages route there.

### Two notifiers (parallel to slack_outbox)

```yaml
notifiers:
  - kind: slack_outbox
    outbox_path: .autosentry/slack_outbox.jsonl
    channel: "C0A4UK987ND"
    thread_key: "pipeline"
  - kind: discord_outbox
    outbox_path: .autosentry/discord_outbox.jsonl
    channel: "123456789012345678"          # Discord channel id
    thread_key: "pipeline"
```

The dispatcher daemon can drain either or both (one daemon per
backend / outbox, configured via flags).

### Auto-detection rules

`_detect_backend` precedence (most capable first):

1. `SLACK_BOT_TOKEN` set → `slack_api`
2. `DISCORD_BOT_TOKEN` set → `discord_bot`
3. `SLACK_WEBHOOK_URL` set → `webhook` (Slack)
4. `DISCORD_WEBHOOK_URL` set → `discord_webhook`
5. fallback → `stdout`

`--backend` flag override values gain `discord_webhook` and
`discord_bot`.

## Task ledger

| #  | task                                              | status |
|----|---------------------------------------------------|--------|
| 52 | This plan                                         | done   |
| 53 | DiscordWebhookBackend                             | done   |
| 54 | DiscordBotBackend                                 | done   |
| 55 | CLI + auto-detection                              | done   |
| 56 | DiscordOutboxNotifier                             | done   |
| 57 | Tests                                             | done   |

## Out of scope

- Discord interactive buttons (approve/abort via UI). Slash-command
  text grammar is enough for v1; buttons are a Phase 5 candidate.
- Voice / video / DM channels.
- Reaction-based commands (👍 = approve). Considered, decided text
  commands are clearer for the audit trail.

## Discord API references (May 2026, captured for reviewer sanity)

- Send message: `POST https://discord.com/api/v10/channels/{channel.id}/messages`
- Start thread on message: `POST .../channels/{channel.id}/messages/{message.id}/threads`
- Get messages after: `GET .../channels/{channel.id}/messages?after={message.id}&limit=N`
- Webhook send: `POST {webhook_url}?wait=true&thread_id={thread_channel_id}`

All with `Authorization: Bot {token}` for bot endpoints.
