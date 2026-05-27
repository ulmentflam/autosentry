# Phase 5.5 — healer-aware restart budget

Today the monitor counts every restart equally — a working `kept` fix
burns the same budget as a deterministic-restart loop that keeps
failing. After `max_restarts` the monitor gives up, even if the
healer was about to land the fix.

This phase makes the budget healer-aware: verified fixes reset the
counter, and we force Claude to engage *before* we exhaust restarts.

## Model change

```
state.restarts_total           ── all-time counter, never resets (for audit)
state.restarts                 ── unverified counter, RESETS on kept fix
                                  → used for both give-up + escalation

state.restarts >= max_restarts                   → give up
state.restarts >= escalate_to_claude_after       → next detection forces Claude
                                                   (rule healer skipped)
```

Default `escalate_to_claude_after = max_restarts // 2` (with the
default `max_restarts: 5`, escalation kicks in at 2 unverified
restarts).

## Why "reset" not "decrement"

User-driven semantics: "new runs after heal don't count." A successful
fix means the supervisor is now in a healthy state; subsequent
detections are new problems with a fresh budget. The all-time counter
in `restarts_total` still records the history for `autosentry analyze`.

## What changes mechanically

1. `MonitorState`: add ``restarts_total: int = 0``. ``restarts`` keeps
   its name but its meaning shifts to "unverified restarts since last
   kept fix."
2. ``record_restart`` increments both.
3. ``_tick_verifications``: when an attempt resolves as ``kept``,
   reset ``state.restarts = 0``.
4. ``HealingConfig.escalate_to_claude_after: int | None = None`` —
   ``None`` means "use ``max_restarts // 2``." Allows explicit override.
5. ``Monitor._escalation_active`` flag — set when the threshold is
   first crossed, cleared on the next kept fix. While set,
   ``_fire_detection`` skips ``rule_healer.attempt`` and goes straight
   to ``claude_healer.attempt``.
6. A high-visibility notification ("budget pressure — forcing Claude")
   fires exactly once per escalation episode.

## What doesn't change

- The ledger (``attempts.tsv``) — still records every attempt with the
  source (rule name or `claude`).
- The give-up exit code and message — still based on
  ``state.restarts >= state.max_restarts``.
- ``autosentry analyze`` — reads the ledger, not state counters.

## Task ledger

| #  | task                                                | status |
|----|-----------------------------------------------------|--------|
| 77 | This plan                                           | done   |
| 78 | restarts_total + reset-on-kept                      | done   |
| 79 | Escalation threshold + force-Claude path            | done   |
| 80 | Escalation notification                             | done   |
| 81 | Doctor surfaces healer budget                       | done   |
| 82 | Tests                                               | done   |
| 83 | Bump 0.6.1 + CHANGELOG + README                     | done   |

## Edge cases (and the decisions)

- **Multiple in-flight verifications.** A `kept` for detector A
  resets the budget even if detector B is still mid-verification.
  Rationale: any successful fix means the supervisor is making
  progress; we want to be forgiving.
- **Force-Claude with Claude disabled.** When `escalate_to_claude_after`
  is hit but `healing.claude.enabled` is false (or resolves to
  disabled), we log a clear warning and fall through to the normal
  rule path — give-up will still fire eventually.
- **Multiple escalations in one episode.** Notification fires once
  per "escalation flipped on" event; subsequent detections within
  the same episode don't re-notify. Kept fix clears the flag and
  arms it again.
