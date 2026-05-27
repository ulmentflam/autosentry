# Phase 3 — supervisors, dispatcher, TUI, web viewer

Picks up where Phase 2 (OSS housekeeping, installer/updater, AI-tool skills)
left off. The unifying theme: replace the Phase 1 stubs with real
implementations, and add the two visualization surfaces (TUI + web).

## Goals

1. Real `slurm`, `docker`, and `attach` supervisors plugged into the
   existing `Supervisor` protocol.
2. A long-running `autosentry dispatcher` daemon that actually delivers
   slack outbox entries (no more "skeleton").
3. A live `autosentry watch` TUI for operators tailing the monitor in a
   second pane.
4. An `autosentry web` static incident viewer — folder-per-incident is
   already a great audit trail; a browsable UI makes triage tolerable.

## Task ledger

| #  | task                                                          | owner  | status |
|----|---------------------------------------------------------------|--------|--------|
| 24 | This plan doc                                                 | claude | done   |
| 25 | SLURM supervisor                                              | claude | done   |
| 26 | Docker supervisor                                             | claude | done   |
| 27 | Attach supervisor                                             | claude | done   |
| 28 | Slack dispatcher daemon (webhook / Slack API / stdout, BIDIR) | claude | done   |
| 29 | Status TUI (`autosentry watch`)                               | claude | done   |
| 30 | Web incident viewer (`autosentry web`)                        | claude | done   |
| 31 | 0.3.0 bump + README/CHANGELOG                                 | claude | done   |

## Design notes

### SLURM supervisor

Configuration goes under `process.extra`:

```yaml
process:
  kind: slurm
  command: ["sbatch", "slurm/submit_train.sh"]   # the submit invocation
  extra:
    log_pattern: "logs/job_{job_id}.log"          # tailed file path
    status_command: ["squeue", "--noheader", "--format=%T", "-j", "{job_id}"]
    cancel_command: ["scancel", "{job_id}"]
    sacct_command:  ["sacct", "-X", "-n", "-o", "State", "-j", "{job_id}"]
    poll_interval_seconds: 10
```

- Parse the job ID from sbatch stdout (`Submitted batch job NNN`).
- `status()` first checks squeue (running/pending); falls back to sacct
  (completed/failed) when squeue returns nothing.
- `iter_log_lines()` opens the log file once it appears, follows it
  (handles rotation by re-opening on fstat changes), yields `None` on
  quiet ticks.
- `apply_action(restart)` = scancel + re-sbatch; `restart_with_env` =
  same, with env propagated via `--export=` and the original env vars.

### Docker supervisor

```yaml
process:
  kind: docker
  command: ["docker", "run", "--rm", "-d", "myimage:latest"]
  extra:
    container_name: "autosentry-train"            # defaults to autosentry-<8char>
    log_stream_command: ["docker", "logs", "-f", "--tail", "0", "{name}"]
    stop_command: ["docker", "stop", "--time", "30", "{name}"]
    remove_command: ["docker", "rm", "-f", "{name}"]
```

- Run command must produce a container ID on stdout when `-d` is used.
- Streams logs via subprocess Popen reading `docker logs -f`.
- restart = stop + rm + re-run.

### Attach supervisor

```yaml
process:
  kind: attach
  command: []
  extra:
    pid: 41822                                    # OR pid_file: "/path/to/pid"
    log_path: "/var/log/myservice.log"
    allow_kill: false                             # if true, apply_action(abort) sends SIGTERM
```

- `start()` is a no-op for an already-running process; if the PID isn't
  alive, emit a warning and exit.
- `status()` polls `os.kill(pid, 0)`.
- `iter_log_lines()` tails the log file (seek to end at start, then
  follow; re-open on inode change).
- `apply_action(restart)` raises a clear error — attach mode can't
  restart anything it didn't start.

### Slack dispatcher daemon (bidirectional)

A standalone subcommand: `autosentry dispatcher run`. **Bidirectional** —
it both pushes outbound messages and pulls inbound replies so an
on-call human can issue commands from inside Slack.

**Outbound.** Reads `slack_outbox.jsonl`, delivers each unsent entry
through a configured backend, marks sent, rewrites the outbox
atomically.

Backends:

- `webhook` (default if `SLACK_WEBHOOK_URL` set): POST to a Slack
  incoming webhook URL. Per-thread limitation: incoming webhooks can't
  thread; degrade to "all in one channel" or split by channel. Inbound
  not supported on webhook backend (Slack doesn't expose it without an
  app token).
- `slack_api`: Slack Web API with `SLACK_BOT_TOKEN`. Supports threads
  AND `conversations.replies` polling for inbound.
- `stdout`: prints; suitable for an MCP-driven session where a Claude
  shell with MCP Slack tools polls both directions on the dispatcher's
  behalf. The dispatcher writes a marker file the MCP session watches.

**Inbound.** Each loop iteration, the dispatcher polls the parent
thread for replies via `conversations.replies` (slack_api backend) or
emits a "poll please" marker (stdout / MCP backend). Captured human
messages get appended to `.autosentry/slack_inbox.jsonl`:

```jsonl
{"id":"…","ts":"2026-05-26T14:35:00Z","user":"U123","text":"abort","thread_ts":"…"}
{"id":"…","ts":"2026-05-26T14:36:11Z","user":"U123","text":"set max_restarts 10"}
```

The monitor reads `slack_inbox.jsonl` on each tick and applies
recognized commands:

- `abort` → stop the supervised process and shut down the monitor.
- `pause` → stop the process but keep the monitor alive.
- `resume` → start the process if it isn't running.
- `set max_restarts N` → update state.max_restarts.
- `approve` → ack the pending Claude proposal (future hook).
- `comment: <free text>` → attach as a note to the next incident.

State (thread_key → thread_ts, last seen reply id per thread) persists
in `.autosentry/dispatcher_state.json`. The dispatcher de-dupes by
message ts so replies aren't re-processed across restarts.

Config selection: `--backend webhook|slack_api|stdout`; env vars
`SLACK_WEBHOOK_URL`, `SLACK_BOT_TOKEN`. Inbound polling rate:
`--poll-seconds 5` (default).

### Status TUI

`autosentry watch [--refresh 1.0]` using `rich.Live`:

```
┌── autosentry ──────────────────────────────────────────────┐
│ pid 41822 · uptime 4h 17m · restarts 2/5 · last 4 min ago  │
├── recent incidents ────────────────────────────────────────┤
│ 2026-05-26T14:32 error/traceback  oom_halve_batch  restart │
│ 2026-05-26T12:09 anomaly/stall    stall_restart    restart │
├── detectors ───────────────────────────────────────────────┤
│ oom         last fired 4m ago    cooldown ok               │
│ training_stall  no hits in 23h                              │
├── log tail ────────────────────────────────────────────────┤
│ [2026-05-26 18:04:51 UTC] [INFO] step 12489/22888 ...      │
│ [2026-05-26 18:04:52 UTC] [INFO] step 12490/22888 ...      │
└────────────────────────────────────────────────────────────┘
```

### Web incident viewer

`autosentry web [--host 127.0.0.1 --port 8765]`. Plain `http.server`
subclass; serves three routes:

- `GET /` — redirect to `/incidents`.
- `GET /incidents` — list (newest first), filter chips for kind/detector,
  search box on message.
- `GET /incidents/<id>` — full rendered `report.md` (Markdown → HTML via
  `markdown-it-py`), with sidebar links to `frames/`, `trace.txt`,
  `configs/`, `state.json`, `fix/`.

Read-only; no auth (bind localhost by default; print a notice if `--host
0.0.0.0`). Renders entirely server-side; no JS framework.

## Open questions

- Do we ship a systemd unit (or launchd plist) example for the dispatcher
  daemon? Plan: yes, as `examples/systemd-dispatcher.service`.
- Web viewer auth? Plan: punt — bind localhost and tell the user to
  tunnel if they need remote.
- TUI: any keyboard shortcuts beyond `q` to quit? Plan: `r` to force a
  refresh, that's it.

## Out of scope for Phase 3

- Real-time push from monitor → TUI (TUI just re-reads state every tick).
- Web UI for editing config / rules — read-only is enough; if you want
  to edit, use the YAML file.
- PyPI release automation (separate task; Phase 4 candidate).
