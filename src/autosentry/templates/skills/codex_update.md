# /autosentry-update — Codex CLI

Check whether autosentry is behind and upgrade it. Confirm before installing.

```bash
command -v autosentry && autosentry --version
autosentry update --check
```

Flow:

1. `autosentry update --check` (add `--no-cache` for live, `--json` to
   parse). Prints current vs latest + the upgrade command when behind.
2. Up to date → report the version, stop.
3. Behind → run the recommended command (ask first): Homebrew →
   `brew upgrade autosentry`; uv/pipx/pip → `autosentry update`.
   `update` auto-detects the backend (`--method` forces it, `--version`
   pins a release, `--pre` allows pre-releases).
4. `autosentry --version` to confirm the bump.

The CLI and a running monitor are separate processes; the new version
applies on the next `autosentry run`. One sentence per step.
