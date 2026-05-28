# /autosentry-update — Cursor

Check for and apply an autosentry upgrade. Confirm before installing.

```bash
command -v autosentry && autosentry --version
autosentry update --check       # cached 1 day; exits 0 either way
```

Steps:

1. `autosentry update --check` (`--no-cache` for a live query, `--json` to
   parse). It prints current vs latest and the upgrade command when behind.
2. **Up to date?** Report the version and stop.
3. **Behind?** Run the recommended command, ask first: Homebrew →
   `brew upgrade autosentry`; uv/pipx/pip → `autosentry update`.
4. `autosentry --version` to confirm.

Notes: the CLI and a running monitor are separate processes — the new
version applies on the next `autosentry run`. Pin with
`autosentry update --version X.Y.Z`; pre-releases with `--pre`.

One sentence per step.
