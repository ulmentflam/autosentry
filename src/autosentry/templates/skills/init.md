# /autosentry-init

Focused skill for **adding autosentry to the current repo**. Use this
when you only want to onboard a fresh repo and don't need the full
`/autosentry` operator playbook.

When invoked:

1. Check if `autosentry` is on PATH. If not, suggest the one-liner:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh
   ```

   Confirm with `autosentry --version`. Do not install silently — ask
   the user before running it.

2. Check for an existing `autosentry.yaml`. If one exists, run
   `autosentry init --upgrade` instead of clobbering. If not, run
   `autosentry init --non-interactive` to scaffold the skeleton.

3. **Read the repo** to figure out what to supervise. Look at
   `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, a
   `Makefile`, or a `scripts/` directory. Propose a `process.command`
   value to the user — **ask them to confirm before editing
   `autosentry.yaml`**. Don't guess at training commands or service
   entry points without checking.

4. Propose `config_snapshots`. Sensible candidates: `.env`,
   `configs/*.yaml`, `pyproject.toml`. Only include files that exist.

5. Propose a starting set of detectors and rules tailored to the
   stack you observed:

   - ML training / Python: `oom` (CUDA out of memory / OutOfMemoryError),
     `nccl`, `traceback`, `stall` with a `metric_regex` matching the
     training loop's progress format (e.g. `step (\\d+)`).
   - Web service / Node / Go: HTTP `5xx` patterns, connection-refused,
     `traceback`, `stall` with no progress regex (no-output threshold).
   - ETL / batch: stage-transition markers, partial-file detectors.

   Keep rules conservative — `restart` for transient failures,
   `restart_with_env: BATCH_SIZE: half` for OOM. Anything else falls
   through to Claude.

6. Run `autosentry skills install --tool agents` so future sessions
   in this repo find AGENTS.md and pick up the full `/autosentry`
   playbook. Optionally `--scope global` to make this the default
   across every repo.

7. Run `autosentry doctor`. If any check is red, fix it. If yellow,
   summarize the warnings.

8. Tell the user the exact `nohup autosentry run > /dev/null 2>&1 &`
   command to start the monitor. **Do not run it yourself.** Hand off
   to the user.

## Bounds

- Don't run `autosentry run` yourself — it's a long-running process
  the user controls.
- Don't overwrite `autosentry.yaml` without explicit consent.
- Don't add detectors or rules without asking first.
- One-line responses per step. The user is operating; you're
  configuring.

## When in doubt

Reference `AGENTS.md` at the repo root if it exists. It has the full
playbook; this file is just the init slice.
