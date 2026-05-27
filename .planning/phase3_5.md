# Phase 3.5 — lazy dispatcher

Tightens the dispatcher daemon so it stops doing ambient work when nothing
is happening. Reply polling is tied to the monitor's anomaly-detection
cycle (the natural "something interesting just happened" beat) and the
outbox drain is mtime-gated.

## Motivation

After Phase 3, the dispatcher daemon polled the Slack thread on a
constant interval (default 5s) whether or not anything had happened.
That's a chatty network call against the Slack API for no reason during
long quiet stretches. Same for the outbox: we read + rewrite the file
every tick even when no new messages were queued.

## Contract

```
                    monitor                          dispatcher
                       │                                  │
   tick ───────────────┤                                  │
   │ run detectors     │                                  │
   │                   │                                  │
   │ if detection:     │   .autosentry/slack_outbox.jsonl │
   │   notifier.notify ├──────────────────────────────►   │  mtime ↑
   │                   │                                  │  → drain
   │   touch(marker)   │   .autosentry/inbox_poll_request │
   │                   ├──────────────────────────────►   │  mtime ↑
   │                   │                                  │  → poll Slack
   │                   │                                  │  → write inbox
   │ read inbox.jsonl  │   .autosentry/slack_inbox.jsonl  │
   │ apply commands    │ ◄────────────────────────────────┤
   │                   │                                  │
```

Three signals between the two:

1. **`slack_outbox.jsonl`** — the monitor's notifier appends; the
   dispatcher drains. **mtime-gated** so the dispatcher reads only when
   the file actually changed.

2. **`inbox_poll_request`** (marker file) — the monitor `touch()`es it
   on every detection fire (and once at startup). The dispatcher checks
   the mtime each loop iteration; when it advances, it polls the Slack
   thread immediately. Otherwise it polls only after `idle_inbound_seconds`
   (default 300s) as a long-period sweep so commands sent in quiet
   stretches still land eventually.

3. **`slack_inbox.jsonl`** — the dispatcher appends. The monitor reads it
   once per tick (cheap, local-only) and applies any recognized commands.
   ``state.last_processed_inbox_id`` tracks progress for resume across
   restarts.

## What the monitor does with commands

| command            | effect                                                |
|--------------------|-------------------------------------------------------|
| `abort`            | stop the supervisor; set monitor's stop flag          |
| `pause`            | stop the supervisor; keep monitor running             |
| `resume`           | start the supervisor if it isn't running              |
| `set max_restarts N` | update state.max_restarts                           |
| `approve`          | record (placeholder for future Claude-approval hook)  |
| `comment: <text>`  | record as a free-form note in state                   |
| (unrecognized)     | logged, ignored                                       |

All commands log at `RECOVERY` level so they show up in the structured
log and the TUI.

## Defaults that changed

- Dispatcher `--poll-seconds` default: 5s → 30s (the loop is so cheap now
  that this is just the file mtime-check cadence).
- New: `--inbound-marker .autosentry/inbox_poll_request` (default).
- New: `--idle-inbound-seconds 300` (5-minute long-period sweep).

## Task ledger

| #  | task                                              | status |
|----|---------------------------------------------------|--------|
| 32 | This plan                                         | done   |
| 33 | mtime-gated outbox drain                          | done   |
| 34 | Marker-triggered inbound polling                  | done   |
| 35 | Monitor-side inbox consumer (inbox.py module)     | done   |
| 36 | Monitor touches marker on detection fire          | done   |
| 37 | Bump 0.3.1 + CHANGELOG + README updates           | done   |
