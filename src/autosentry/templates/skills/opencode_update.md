---
description: Check whether autosentry is behind the latest PyPI release and upgrade it in place, using the right backend.
agent: build
---

# /autosentry-update — OpenCode

Check for and apply an autosentry upgrade. The condensed flow:

1. `autosentry update --check` → prints current vs latest; add `--no-cache`
   to force a live PyPI query, `--json` to parse.
2. Up to date → report the version and stop.
3. Behind → run the recommended command (ask first): Homebrew →
   `brew upgrade autosentry`; uv/pipx/pip → `autosentry update`.
4. `autosentry --version` to confirm.

The CLI and a running `autosentry run` monitor are separate processes —
the new version applies on the next `autosentry run`. One sentence per
step. Don't narrate the docs.
