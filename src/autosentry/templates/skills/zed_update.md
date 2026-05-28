# /autosentry-update — Zed

Check for and apply an autosentry upgrade. Confirm before installing.

```bash
command -v autosentry && autosentry --version
autosentry update --check
```

Steps:

1. `autosentry update --check` (`--no-cache` for live, `--json` to parse).
   Prints current vs latest + the upgrade command when behind.
2. Up to date → report the version, stop.
3. Behind → run the recommended command (ask first): Homebrew →
   `brew upgrade autosentry`; uv/pipx/pip → `autosentry update`.
4. `autosentry --version` to confirm.

The CLI and a running monitor are separate processes; the new version
applies on the next `autosentry run`. One sentence per step. The full
operator playbook is in `/autosentry` (not this skill).
