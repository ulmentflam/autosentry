# Phase 4 — autoresearch-inspired self-iterating fixes

Takes the things autoresearch does well — **git as the source of truth**,
**outcome-driven keep/discard**, **bounded budgets**, **append-only TSV
ledger** — and applies them to autosentry's "Claude is editing my code"
problem. Today autosentry captures a diff into the incident folder but
leaves the working tree dirty and doesn't know whether the fix worked.
Phase 4 closes that loop.

Reference repo:
<https://github.com/ulmentflam/autoresearch>.

## What changes for the user

```
detection fires
   │
   ▼
healer chooses a fix (rule or Claude)
   │
   ▼  (if Claude / if config asks for branching)
git switch -c autosentry/fix-<incident-id>
   │
   ▼
Claude edits files on the branch
   │
   ▼
supervisor applies action (restart_with_env / restart / …)
   │
   ▼
monitor watches for the same detector to re-fire within verify_window_seconds
   │
   ├── doesn't fire → mark KEPT, optionally fast-forward main, delete branch
   ▼
fires again → mark REGRESSED, revert to main (branch kept as artifact),
              optionally escalate / increment per-detector budget
```

All decisions are written to `attempts.tsv`. `autosentry analyze` reads
that file plus the existing `incidents/index.jsonl` and produces a flat
summary an operator can grep / scan.

## Contracts

### `attempts.tsv`

Append-only, tab-separated, header row. Columns:

```
timestamp  incident_id  detector  source       branch                       status      duration_seconds  description
```

- `source`: rule name OR `claude`.
- `status`: `pending` | `kept` | `discarded` | `crashed` | `regressed`.
- A row starts as `pending` when the fix is applied and is updated to
  one of the terminal states by the verifier (atomic rewrite of the
  file).

### Fix branches

Naming: `<branch_prefix>/<incident-id>` (default `autosentry/fix-`).
Created with `git switch -c` from the current HEAD. Edits captured into
the incident folder's `fix/diff.patch` come from the same diff, but now
the branch is the durable artifact.

After verification:

- **kept** → if `healing.git.auto_merge: true`, `git switch main && git
  merge --ff-only <branch>`. Branch deleted. Otherwise: branch left,
  operator merges by hand.
- **regressed** → `git switch main && git restore .` to clean working
  tree. Branch kept (named with the incident id) for forensic review.
- **crashed** (the supervised process exited badly during verification)
  → treated as regressed.

### Healer budget

Per-detector rolling window. If a detector has fired N times in the
last H hours and each fix attempt has been `regressed` or `crashed`,
autosentry stops attempting fixes for that detector — instead it just
writes the incident and notifies. The budget resets after a manual
`approve` command (via the Slack inbox) or after H hours of quiet.

Config (defaults shown):

```yaml
healing:
  git:
    enabled: true
    branch_prefix: "autosentry/fix-"
    auto_merge: false
  budget:
    max_attempts_per_detector_per_hour: 5
    max_wall_seconds_per_incident: 600
  verify_window_seconds: 600
  regression_action: revert   # revert | escalate | ignore
```

### `program.md`

Scaffolded into `.autosentry/program.md` by `autosentry init`. Codifies
the autonomous-operator loop: "set up the watcher, configure detectors,
start, never stop, react to incidents via the ledger." Distinct from
`recovery.md`, which is invoked per-incident; `program.md` is the
agent's runtime mission statement.

### `autosentry analyze`

Reads `attempts.tsv` + `incidents/index.jsonl`. Prints:

```
top failing detectors (last 24h)
  training_stall    14
  oom               5
  exit_code         3

per-rule success rate
  oom_halve_batch   80% (4/5)
  transient_restart 100% (3/3)

regression streaks
  training_stall: 3 in a row
```

Flag `--since 24h` to window. Flag `--format json` for machine reading.

## Task ledger

| #  | task                                                | status |
|----|-----------------------------------------------------|--------|
| 38 | This plan                                           | done   |
| 39 | Fix-branch isolation                                | done   |
| 40 | Healer budget                                       | done   |
| 41 | Attempts ledger (attempts.tsv)                      | done   |
| 42 | Outcome verification                                | done   |
| 43 | program.md scaffolded skill                         | done   |
| 44 | `autosentry analyze` command                        | done   |
| 45 | Bump 0.4.0 + CHANGELOG + README                     | done   |

## What we're NOT building

- A swarm of Claude instances. autoresearch is single-agent; so is
  autosentry. One incident, one fix attempt.
- A `analysis.ipynb`-style notebook. A flat-text `analyze` command is
  enough and doesn't bring jupyter into the dep tree.
- Branching outside of git repos. If the supervised project isn't a git
  repo, fix-branching is a no-op and we fall back to today's diff-only
  behavior with a warning.
