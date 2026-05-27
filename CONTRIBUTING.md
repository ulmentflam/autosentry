# Contributing to autosentry

Thanks for considering a contribution. autosentry is a small, opinionated tool —
the goal is a sentry that's easy to read, easy to extend, and hard to break.
Contributions that keep it that way are welcome.

## Ground rules

- **Be additive.** Don't break the public CLI surface (`autosentry init`,
  `autosentry run`, etc.) or the YAML schema without discussion.
- **Be honest in incident reports.** Anything autosentry writes to
  `.autosentry/incidents/` becomes a load-bearing audit trail for operators —
  if you can't justify a change to the report shape, don't make it.
- **Trust internal code.** Don't add error handling, fallbacks, or validation
  for scenarios that can't happen. Validate at system boundaries (user config,
  log streams, subprocess returns).
- **No comments-as-narrative.** Code should explain itself; comments are for
  *why*, not *what*.

## Development setup

```bash
git clone https://github.com/ulmentflam/autosentry.git
cd autosentry
make install        # uses uv when present; falls back to pip
                    # also installs the pre-commit hook if pre-commit is on PATH
make ci             # ruff lint + format check + pyrefly + pytest
```

`make install` auto-registers `.git/hooks/pre-commit` so every commit
runs `ruff check --fix`, `ruff format`, and `pyrefly check src/autosentry`
against your changes — the same checks CI runs. If you skipped that
step or are picking up a fresh clone:

```bash
make hooks          # or: pre-commit install
```

Skip the hooks for a single commit only when you have a real reason:

```bash
git commit --no-verify
```

> macOS / iCloud Drive users: if your clone is under
> `~/Library/Mobile Documents/`, the Makefile automatically points the venv at
> `~/.cache/autosentry-venv` to dodge iCloud's `UF_HIDDEN` flag. Override with
> `make install VENV=…`.

### Useful targets

| target          | what it does                                       |
|-----------------|----------------------------------------------------|
| `make install`  | install package + dev deps                         |
| `make format`   | ruff format                                        |
| `make lint`     | ruff check + ruff format --check                   |
| `make lint-fix` | ruff check --fix + ruff format                     |
| `make typecheck`| pyrefly                                            |
| `make test`     | pytest                                             |
| `make test-cov` | pytest with coverage                               |
| `make ci`       | lint + typecheck + test (what CI runs)             |
| `make build`    | sdist + wheel                                      |

## Submitting changes

1. Open an issue first for anything non-trivial (new supervisor backend, new
   detector, schema change). For typos and one-line bug fixes, a PR is fine.
2. Branch off `main`. Keep PRs focused — one concern per PR.
3. `make ci` must pass.
4. New behavior needs a test. Bug fixes need a regression test.
5. Update `CHANGELOG.md` under the `## [Unreleased]` section.
6. Write a clear commit message (imperative, present tense). Sign off with
   `Co-Authored-By:` if AI tools helped — we keep that visible on purpose.

## Designing a new component

Each layer is small and pluggable. If you're adding to one:

- **Supervisor** — implement `Supervisor` from `supervisors/base.py`. Required:
  `start`, `stop`, `status`, `iter_log_lines`, `apply_action`. Wire into
  `supervisors/__init__.py::supervisor_for`.
- **Detector** — subclass `Detector` from `detectors/base.py`. Implement at
  least `observe_line`; override `observe_status` / `observe_tick` for
  state-driven or time-driven detectors. Wire into
  `detectors/__init__.py::build_detectors`.
- **Healer** — match the implicit protocol of `RuleHealer.attempt(det) ->
  HealerOutcome | None`. Healers don't apply actions; the monitor does.
- **Notifier** — subclass `Notifier` from `notifiers/base.py`. Wire into
  `notifiers/__init__.py::build_notifiers`.

## Reporting bugs

Use the issue template. Include:
- `autosentry --version`
- The relevant slice of your `autosentry.yaml`
- The latest incident folder (or the contents of `.autosentry/logs/autosentry.log`)
- A minimal repro if you can — failing subprocess + config is ideal.

## License

By contributing you agree your contributions are licensed under the
[Apache License 2.0](./LICENSE), the same terms as the rest of the project.
