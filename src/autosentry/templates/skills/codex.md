# /autosentry — Codex CLI

Use this prompt when the user types `/autosentry` inside the OpenAI
Codex CLI. The complete playbook lives at `AGENTS.md` at the repo
root — read it first.

## Quick triage

```bash
command -v autosentry && autosentry --version
[ -f autosentry.yaml ] && echo configured
[ -f .autosentry/state.json ] && cat .autosentry/state.json
autosentry update --check   # newer release? (cached daily)
```

- If `autosentry update --check` prints `→ update available` → mention it
  once and recommend the command it shows (`autosentry update`, or
  `brew upgrade autosentry` for Homebrew). Don't upgrade unprompted.
- Missing CLI → suggest the install one-liner from the README.
- Missing config → `autosentry init`, then configure
  `process.command`, `config_snapshots`, detectors, and rules.
- Missing state → launch in background:
  `nohup autosentry run > /dev/null 2>&1 &`.
- Running → use `autosentry status`, `autosentry watch`,
  `autosentry web`, `autosentry incidents list/show`,
  `autosentry analyze --since 24h`.

## Authoring rules

When the same detector has escalated to Claude 3+ times with similar
fixes that all verified, codify a YAML rule:

```yaml
- name: descriptive_name
  match: { detector: <detector_name>, message_regex: "..." }
  action: { kind: restart_with_env, set: { KEY: value } }
```

Insert into `autosentry.yaml` under `rules:` before any catch-all.

Keep responses tight. One or two sentences per action.
