---
description: Check whether autosentry is behind the latest PyPI release and upgrade it in place, using the right backend (uv / pipx / pip / Homebrew).
argument-hint: ""
allowed-tools: Bash, Read
---

# /autosentry-update — Claude Code

Check for and apply an autosentry upgrade. Confirm before installing.

## Pre-flight

```bash
command -v autosentry && autosentry --version
autosentry update --check        # cached 1 day; exits 0 whether or not behind
```

## Steps

1. **Check.** Run `autosentry update --check` (add `--no-cache` to force a
   live PyPI query, or `--json` for `{current, latest, is_outdated}`). It
   prints current vs latest and, when behind, the exact upgrade command.

2. **Up to date?** Report the version and stop — nothing to do.

3. **Behind?** Run the command it recommended (ask the user first):
   - Homebrew install → `brew upgrade autosentry`
   - uv tool / pipx / pip install → `autosentry update`

   `autosentry update` auto-detects the backend; force it with
   `--method uv|pipx|pip|brew` if detection is wrong, pin a release with
   `--version X.Y.Z`, or allow pre-releases with `--pre`.

4. **Verify.** Re-run `autosentry --version` to confirm the new version
   landed.

## Notes

- The CLI and a running `autosentry run` monitor are separate processes —
  upgrading the CLI doesn't disturb a live monitor; the new version takes
  effect on the next `autosentry run`.
- For an editable/dev checkout (`pip install -e .`), `autosentry update`
  won't touch your working tree — use `git pull` + reinstall instead.

One sentence per step. The user drives.
