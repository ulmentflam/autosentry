# Phase 5 — interactive healer with subagent routing

Subprocess Claude is fine for headless deployments but doesn't fit the
common case anymore: users launch autosentry from inside an open
Claude Code session, want the healer's fallback to use *that* session
(or subagents spawned from it), and don't want to install or maintain
the `claude` CLI separately.

This phase adds a second healer mode — **interactive** — that does a
file-based request/response handshake with whatever Claude session is
running the `/autosentry` skill. The skill, given the request file,
decides which subagent type to spawn (configurable per detector) and
hands off the diagnosis.

## Mode resolution

```
healing.claude.mode:
  auto         ──► resolve at startup
  subprocess   ──► always spawn `claude --print`
  interactive  ──► always use the file handshake

auto resolution:
  /autosentry skill present in repo?   ──► interactive
  `claude` on PATH?                    ──► subprocess
  neither                              ──► healer skipped, rule-only
```

`healing.claude.enabled` accepts `true | false | "auto"`. `auto` is the
new default and follows the table above; `false` is the unconditional
opt-out.

## Subagent routing

```yaml
healing:
  claude:
    enabled: auto
    mode: auto
    subagents:
      default:
        type: "general-purpose"
        description: "Diagnose an autosentry incident"
      training_stall:
        type: "general-purpose"
        description: "Diagnose a stalled training loop"
      oom:
        type: "general-purpose"
        description: "Resolve an out-of-memory failure"
```

The healer writes the resolved subagent spec into the request file as a
hint; the skill is the actual dispatcher (since only the Claude session
can call the Task tool).

## File contracts

### Request (`.autosentry/recovery_request.md`)

```markdown
---
incident_id: 2026-05-26T14-32-10Z-error-traceback
detector: training_stall
subagent:
  type: general-purpose
  description: Diagnose a stalled training loop
timeout_seconds: 600
---

<existing recovery prompt body — same content as today's subprocess prompt>
```

YAML frontmatter so the skill can parse the routing metadata without
running it through Claude. ts-like format → easy to grep.

### Response (`.autosentry/recovery_response.md`)

```
ACTION: restart_with_env
SET: BATCH_SIZE=4

<free-form diagnosis text captured into the incident folder as
claude_response.md>
```

Same parse format as today's `_parse_action`, so the healer can reuse
its existing regex.

## `autosentry healer respond` CLI

The subagent doesn't need to author the markdown by hand:

```bash
autosentry healer respond \
  --action restart_with_env \
  --set BATCH_SIZE=4 \
  --diagnosis "OOM at step 8450. Halving batch."
```

Writes `.autosentry/recovery_response.md` in the format above, with
`autosentry healer respond` as the single tool the subagent learns.

## Skill change

`/autosentry` skill grows a third phase: an "interactive watcher" loop
that polls the request marker, spawns subagents, and produces responses.
Documented as something the user runs as a background prompt inside
their open Claude Code session:

> While the monitor is running, watch for `.autosentry/recovery_request`
> mtime changes. When it advances, parse the YAML frontmatter, spawn
> a subagent of the type listed under `subagent.type` with the body as
> its prompt, then `autosentry healer respond …` with the subagent's
> conclusion.

## Task ledger

| #  | task                                                | status |
|----|-----------------------------------------------------|--------|
| 69 | This plan                                           | done   |
| 70 | Config schema (enabled/mode/subagents)              | done   |
| 71 | ClaudeHealer refactor (subprocess + interactive)    | done   |
| 72 | `autosentry healer respond` CLI                     | done   |
| 73 | Skill update (poll + subagent + respond loop)       | done   |
| 74 | Doctor: report resolved healer mode                 | done   |
| 75 | Tests                                               | done   |
| 76 | Bump 0.6.0 + CHANGELOG + README                     | done   |

## Out of scope

- Real-time push to the Claude session (we rely on the skill's polling
  loop; that loop is the user's responsibility to keep running, just
  like the dispatcher daemon).
- Subagents spawning subagents — we pass one type per incident.
- Migration of the subprocess path: it stays as a first-class mode
  forever, since headless deployments (k8s, CI, no-network boxes)
  need it.
