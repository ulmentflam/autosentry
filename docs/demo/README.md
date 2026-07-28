# README demo

Source for `docs/demo.gif` — the GIF at the top of the main README.

The demo is a real autosentry run, not a mock-up. `train.py` genuinely
runs out of memory at `BATCH_SIZE=8` and genuinely succeeds at `4`, so
the `oom_halve_batch` rule has an actual failure to fix. If the healing
path breaks, re-rendering the GIF fails to show a fix — which is the
point of keeping it reproducible.

## Files

| File | What it is |
|---|---|
| `demo.tape` | [VHS](https://github.com/charmbracelet/vhs) script — the source of truth for the recording |
| `train.py` | Stand-in training script: OOMs at `BATCH_SIZE >= 8`, completes below it |
| `autosentry.yaml` | Demo config — one `oom` pattern detector, one rule, Claude disabled |

## Regenerating

Needs `vhs` (which pulls in `ttyd` + `ffmpeg`) and `gifsicle`:

```bash
brew install vhs gifsicle
```

From the repo root:

```bash
make install                                          # so `autosentry` is the local build
PATH="$(make -s venv-info)/bin:$PATH" vhs docs/demo/demo.tape
gifsicle -O3 --lossy=30 --colors 64 docs/demo.gif -o docs/demo.gif
```

The tape puts `PATH` in front deliberately: it records whatever
`autosentry` resolves to, so without this the GIF would silently capture
a stale globally-installed version instead of the code in this checkout.

The recording runs out of `/tmp/autosentry-demo` (set up and torn down
inside the tape) rather than the repo, so the supervisor's absolute
`cwd=` stays short enough to read at GIF width and each take starts from
a clean state directory.

## If you change the demo

Keep it under ~25 seconds and re-run `gifsicle` — the committed GIF
should stay well under 1 MB so the README loads fast. Check the result
before committing; a truncated GIF renders as a broken image on GitHub
and is easy to miss locally.
