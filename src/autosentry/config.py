"""Config schema for ``autosentry.yaml``.

The YAML is validated through pydantic models. ``load_config`` resolves
relative paths against the *project root* — the directory that contains
``.autosentry/`` — and interpolates ``$ENV_VAR`` references in
``process.command``, ``process.env``, and the healer command.

The config lives at ``.autosentry/autosentry.yaml`` so a single
``.autosentry/`` entry git-ignores every autosentry file at once. A
root-level ``autosentry.yaml`` (the pre-0.8 layout) is still honored as
a fallback so existing repos keep working without a migration.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, Field, field_validator
from ruamel.yaml import YAML

ProcessKind = Literal["local", "slurm", "docker", "attach"]

#: Where ``autosentry init`` writes the config and where every command
#: looks first. Kept inside ``.autosentry/`` so the whole tree is one
#: git-ignorable directory.
DEFAULT_CONFIG_PATH = Path(".autosentry/autosentry.yaml")

#: Pre-0.8 location — a bare ``autosentry.yaml`` at the repo root. Still
#: loaded as a fallback (see :func:`load_config`) for repos initialized
#: before the move.
LEGACY_CONFIG_PATH = Path("autosentry.yaml")


class RestartPolicy(BaseModel):
    # ``max_restarts`` is *not* a raw cap — it's the kill-switch for
    # consecutive **unverified** restarts. The counter at
    # ``state.restarts`` zeros on every fix attempt that resolves as
    # ``kept`` in the attempts ledger, so a productive healer keeps the
    # supervisor running indefinitely; only a healer that can't land a
    # kept fix for ``max_restarts`` restarts in a row trips this.
    #
    # Default 50: high enough that long-running ML workloads (where
    # transients heal but slowly) never trip it, low enough that a
    # truly broken healer eventually stops thrashing instead of
    # spamming the API forever. Set to ``0`` to disable the kill-switch
    # entirely (the supervisor will keep restarting until something
    # external stops it).
    max_restarts: int = 50
    backoff: Literal["fixed", "exponential"] = "exponential"
    cooldown_seconds: int = 60


#: How the monitor treats a child's clean exit (code 0).
#:
#: - ``restart_on_failure`` (default): clean exit ends the supervisor; non-zero
#:   exit goes through the healer/restart_policy path. Most users want this —
#:   it preserves the pre-0.8.5 healing behavior without leaving the supervisor
#:   hanging idle after a successful one-shot run (issue #5).
#: - ``one_shot``: any exit (clean or not) ends the supervisor. The healer is
#:   never invoked on exit. Use when restart of any kind would be wrong.
#: - ``restart_always``: keeps the pre-0.8.5 behavior — both clean and dirty
#:   exits go through the healer/restart_policy path. Use when a clean exit
#:   genuinely means "loop forever" (e.g. a poller that exits 0 between ticks).
ProcessLifecycle = Literal["restart_always", "restart_on_failure", "one_shot"]


class StageSpec(BaseModel):
    """One stage in a multi-step pipeline (e.g. pretrain → SFT → GRPO).

    Each stage runs sequentially; stage N starts only when stage N-1
    exits cleanly. Restart budgets are per-stage — a stage that burns
    through its budget aborts the pipeline rather than poisoning
    subsequent stages. Detectors/rules are global (defined at the top
    level) and apply to every stage; that keeps the common case
    (same training script, different phases) ergonomic.
    """

    name: str
    command: list[str]
    cwd: str | None = None  # defaults to process.cwd
    env: dict[str, str] = Field(default_factory=dict)
    restart_policy: RestartPolicy | None = None  # falls back to process.restart_policy
    lifecycle: ProcessLifecycle | None = None  # falls back to process.lifecycle

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not v or not v.strip():
            msg = "stage 'name' is required and must be non-empty"
            raise ValueError(msg)
        # Stage names land in file paths (incident tags, vault notes) so
        # keep them simple. Lowercase letters, digits, dashes, underscores.
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", v):
            msg = f"stage name {v!r} must match [A-Za-z0-9_-]+ (no spaces or punctuation)"
            raise ValueError(msg)
        return v

    @field_validator("command")
    @classmethod
    def _validate_command(cls, v: list[str]) -> list[str]:
        if not v:
            msg = "stage 'command' is required and must be non-empty"
            raise ValueError(msg)
        return v

    @field_validator("env", mode="before")
    @classmethod
    def _coerce_env(cls, v):
        return {} if v is None else v


class ProcessConfig(BaseModel):
    kind: ProcessKind = "local"
    command: list[str] = Field(default_factory=list)
    cwd: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    restart_policy: RestartPolicy = Field(default_factory=RestartPolicy)
    # See :data:`ProcessLifecycle`. Default ``restart_on_failure`` so a clean
    # exit no longer leaves the monitor sitting idle indefinitely (#5).
    lifecycle: ProcessLifecycle = "restart_on_failure"
    # Optional pipeline of sequentially-run stages. When set, ``command``
    # must be empty; the PipelineRunner orchestrates Monitor invocations
    # per stage, advancing on exit code 0 and aborting on healer
    # exhaustion. Restart budgets reset between stages (see
    # :class:`autosentry.state.ResetRecord` with source="pipeline").
    stages: list[StageSpec] = Field(default_factory=list)
    # Kind-specific extras live here so we don't need a discriminated union yet.
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extra", "env", mode="before")
    @classmethod
    def _coerce_none_to_empty(cls, v):
        # YAML keys present but empty parse as None (e.g. `extra:` with no
        # children). Treat that as an empty dict so the schema stays tolerant.
        return {} if v is None else v

    @field_validator("stages", mode="before")
    @classmethod
    def _coerce_stages_none(cls, v):
        return [] if v is None else v

    def model_post_init(self, __context: Any) -> None:  # noqa: D401, ANN001
        # Mutual-exclusion between ``command`` and ``stages``: either the
        # single-process flow or the pipeline flow, never both. We accept
        # bare-default ``command=[]`` alongside ``stages`` (a fresh init
        # template ships with an empty command list).
        if self.stages and self.command:
            msg = (
                "process.command and process.stages are mutually exclusive — "
                "use one or the other. (Pipelines should set only "
                "process.stages; each stage carries its own command.)"
            )
            raise ValueError(msg)
        # Stage names must be unique — they're used as keys in pipeline.json
        # and as labels on incidents/notifications.
        if self.stages:
            seen: set[str] = set()
            for s in self.stages:
                if s.name in seen:
                    msg = f"duplicate stage name {s.name!r} in process.stages"
                    raise ValueError(msg)
                seen.add(s.name)

    def is_pipeline(self) -> bool:
        return bool(self.stages)


class MonitorConfig(BaseModel):
    poll_interval_seconds: int = 30
    stall_timeout_seconds: int = 1800
    log_dir: str = ".autosentry/logs"
    log_tail_lines: int = 2000
    log_excerpt_lines: int = 200  # how much to capture into each incident
    # How long the supervisor may sit with no live child before it calls
    # itself wedged, says so loudly, and stops. A supervisor that dies
    # gets restarted by whatever service manager owns it; one that keeps
    # heartbeating over nothing is invisible to every "is it running?"
    # check there is, which is the worst failure shape available for an
    # unattended pipeline (issue #22).
    #
    # This is a backstop, not a normal code path: every route out of a
    # dead child (healer action, restart_policy fallback, lifecycle gate)
    # resolves in seconds, and long healer work blocks the loop rather
    # than ticking through it. 15 minutes leaves room for a slow
    # session-dispatch handoff. ``0`` disables the backstop.
    dead_child_grace_seconds: int = 900


class SourceExplodeConfig(BaseModel):
    enabled: bool = True
    context_lines: int = 10
    languages: list[str] = Field(
        default_factory=lambda: ["python", "javascript", "typescript", "go", "rust"]
    )
    skip_paths: list[str] = Field(
        default_factory=lambda: ["site-packages/", ".venv/", "node_modules/", "/usr/lib/"]
    )
    max_frames: int = 20


class DetectorSpec(BaseModel):
    kind: Literal["pattern", "traceback", "stall", "exit_code"]
    name: str | None = None
    # pattern detector
    regex: str | None = None
    # stall detector
    metric_regex: str | None = None
    no_progress_seconds: int | None = None
    # exit detector
    nonzero_only: bool = True
    # common
    cooldown_seconds: int = 60

    @field_validator("regex")
    @classmethod
    def _compile_regex(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v


class RuleMatch(BaseModel):
    detector: str
    message_regex: str | None = None

    @field_validator("message_regex")
    @classmethod
    def _compile_regex(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v


class RuleAction(BaseModel):
    kind: Literal["restart", "restart_with_env", "pause", "abort", "custom_command"]
    set: dict[str, str] = Field(default_factory=dict)  # for restart_with_env
    command: list[str] = Field(default_factory=list)  # for custom_command
    notify: bool = True


class Rule(BaseModel):
    name: str | None = None
    match: RuleMatch
    action: RuleAction


class SubagentSpec(BaseModel):
    """One subagent type the interactive healer can spawn.

    Maps a detector name (or ``default``) to a Claude Code Task-tool
    subagent type. The healer writes the chosen spec into the
    ``recovery_request.md`` frontmatter; the ``/autosentry`` skill
    running in the user's Claude session reads it and spawns the
    matching subagent via the Task tool.
    """

    type: str = "general-purpose"
    description: str = "Diagnose an autosentry incident"


class ClaudeConfig(BaseModel):
    # ``True | False | "auto"``. ``auto`` resolves based on whether the
    # ``/autosentry`` skill is installed AND/OR ``claude`` is on PATH —
    # see ``ClaudeHealer._resolve_mode``.
    enabled: bool | Literal["auto"] = "auto"
    # ``auto | subprocess | interactive | langgraph``.
    #
    # - ``auto``: picks ``interactive`` when the ``/autosentry`` skill
    #   is installed (FREE under the user's Claude Code subscription;
    #   the preferred path), ``subprocess`` when only ``claude`` is on
    #   PATH, and ``disabled`` (no-op) when neither is available.
    # - ``subprocess``: spawn ``claude --print``. **Billed per call**
    #   against the user's Anthropic API account. Superseded by
    #   ``dispatch.mode: session`` for Claude Code users and by
    #   ``langgraph`` for headless deployments — kept for backwards
    #   compat with a soft deprecation warning at config-load.
    # - ``interactive``: file-handshake with an open ``/autosentry``
    #   session. Free under Claude Code subscription.
    # - ``langgraph``: route to the LangGraph diagnosis graph
    #   (Anthropic / OpenAI / Google providers, BYO API key). See
    #   :class:`LangGraphConfig`. Multi-step with optional cross-check.
    mode: Literal["auto", "subprocess", "interactive", "langgraph"] = "auto"
    command: list[str] = Field(default_factory=lambda: ["claude", "--print"])
    timeout_seconds: int = 600
    prompt_template: str = ".autosentry/prompts/recovery.md"
    include_in_context: list[Literal["state", "last_incident", "config_snapshots"]] = Field(
        default_factory=lambda: cast(
            list[Literal["state", "last_incident", "config_snapshots"]],
            ["state", "last_incident", "config_snapshots"],
        )
    )
    # Interactive-mode file handshake. The healer writes ``request_path``
    # and blocks on ``response_path``'s mtime.
    request_path: str = ".autosentry/recovery_request.md"
    response_path: str = ".autosentry/recovery_response.md"
    # Detector → subagent mapping. ``default`` is the fallback when a
    # detector has no explicit entry. Empty dict → use a built-in default.
    subagents: dict[str, SubagentSpec] = Field(default_factory=dict)


class GitConfig(BaseModel):
    """How autosentry uses git to isolate fix attempts.

    Inspired by autoresearch (https://github.com/ulmentflam/autoresearch):
    each Claude-driven fix attempt lands on its own branch. If the fix
    verifies (the failure doesn't re-fire within ``verify_window_seconds``),
    the branch can be fast-forwarded into ``main``. If the fix regresses,
    the branch stays as a forensic artifact and the working tree is
    restored.
    """

    enabled: bool = True
    branch_prefix: str = "autosentry/fix-"
    # When true and the fix verifies cleanly, fast-forward main and delete
    # the branch. When false (the safe default), the branch is left for the
    # operator to merge by hand.
    auto_merge: bool = False
    # When false, never write to disk; only log the diff. Useful for
    # non-git repos or read-only environments.
    write_enabled: bool = True


class HealerBudget(BaseModel):
    """Per-detector and per-incident bounds on fix attempts.

    Anti-thrash. If a detector keeps firing and every fix attempt
    regresses, autosentry stops trying and just writes incidents +
    notifies, until a manual ``approve`` lands in the slack inbox or
    the rolling window passes.

    Defaults are chosen to be as permissive as possible without
    risking API spam from a stuck-loop healer:

    - ``max_attempts_per_detector_per_hour: 60`` — one healer attempt
      per minute per detector. Generous enough that a long-running
      workload with transient noise never feels capped; bounded enough
      that a detector firing every second still won't burn through
      thousands of Claude calls per hour.
    - ``max_wall_seconds_per_incident: 7200`` — two-hour budget per
      incident. Long enough for slow ML-training crash loops to land
      a kept fix; short enough that one stuck incident doesn't burn
      forever.

    Set either knob to ``0`` to disable that bound entirely.
    """

    max_attempts_per_detector_per_hour: int = 60
    max_wall_seconds_per_incident: int = 7200


class DispatchConfig(BaseModel):
    """Who decides which healer to run for each detection.

    - ``builtin`` (default): the in-process monitor matches rules,
      invokes the Claude healer when no rule applies, and applies
      actions itself. This is the original behavior.
    - ``session``: the monitor stops at incident write — it detects,
      writes the incident folder, updates state, but does not run
      the rule or Claude healer and does not apply any action. An
      interactive Claude Code session (via the ``/autosentry`` skill +
      a ``Stop`` hook running ``autosentry watch --once``) reads the
      pending incidents and dispatches healers via the Task tool.

      The restart_policy safety-net (the fallback that restarts a dead
      child up to ``max_restarts`` times) still runs in session mode —
      we don't want the supervised process staying dead if no session
      is around to react. Session mode disables the *smart* fix path
      (rules + Claude), not the dumb-but-reliable safety net.
    """

    mode: Literal["builtin", "session"] = "builtin"
    # File the monitor touches every time a detection writes an
    # incident under ``mode=session``. The session's Stop hook /
    # ``watch --once`` reads its mtime to know when there's new work.
    request_marker: str = ".autosentry/session_dispatch_request"
    # Where the session writes the last-handled incident id so the
    # next ``watch --once`` only returns truly new work.
    cursor_path: str = ".autosentry/session_dispatch_cursor"
    # Append-only queue the session writes to (via ``autosentry session
    # apply``) to ask the monitor to apply a RuleAction (restart,
    # restart_with_env, …). The monitor drains this on each tick in
    # session mode. Cousin of the recovery_request file dance, but
    # one-directional and per-action instead of one-at-a-time.
    action_queue_path: str = ".autosentry/session_actions.jsonl"
    # Where the monitor records the last-applied action id so it doesn't
    # re-apply on restart.
    action_cursor_path: str = ".autosentry/session_action_cursor"


#: Provider identifier the LangGraph healer accepts. Resolved to a
#: concrete LangChain chat model class by the provider factory.
LangGraphProvider = Literal["anthropic", "openai", "google"]


class LangGraphCrossCheck(BaseModel):
    """Second-LLM validation pass before the proposed action is applied.

    A second model reviews the primary's proposal and replies
    APPROVE / REJECT / MODIFY. REJECT loops back to ``diagnose`` once
    with the cross-checker's feedback (bounded by
    :attr:`LangGraphConfig.max_steps`); MODIFY adopts the modified
    proposal; APPROVE finalizes.
    """

    enabled: bool = False
    provider: LangGraphProvider = "openai"
    model: str = "gpt-4o-mini"


class LangGraphConfig(BaseModel):
    """Headless LLM healer powered by LangGraph.

    Used when ``healing.claude.mode: langgraph`` (or when ``auto``
    resolves to it). This is the **per-call-billed** path for
    deployments without an interactive Claude Code session; the session
    path (``dispatch.mode: session``) is preferred when available
    because it runs free under the user's Claude Code subscription.

    The graph is intentionally small: ``prepare_context → diagnose →
    [cross_check?] → finalize``. ``diagnose`` and ``cross_check`` are
    full LLM calls with bounded tool access (read_file, grep_repo,
    view_log_excerpt); ``finalize`` builds the :class:`HealerOutcome`.
    """

    # Disabled by default — opt-in to avoid surprise billing. The mode
    # selector (``claude.mode: langgraph``) is the other half of the
    # opt-in; both must agree for the healer to fire.
    enabled: bool = False
    provider: LangGraphProvider = "anthropic"
    # Concrete model name; provider-specific. Defaults pick a fast,
    # cheap model suitable for the diagnosis loop — operators tuning
    # for higher-stakes deployments should bump to opus / gpt-5 / etc.
    model: str = "claude-haiku-4-5"
    # Sampling temperature for the diagnosis node. 0 is recommended
    # for deterministic action selection.
    temperature: float = 0.0
    # Maximum diagnose→cross_check→diagnose loops before giving up
    # and returning ``None`` (no action proposed). Each loop is a
    # full LLM round-trip, so this directly bounds cost per incident.
    max_steps: int = 3
    # Per-LLM-call timeout in seconds. Distinct from
    # ``ClaudeConfig.timeout_seconds`` which bounds the whole healer.
    request_timeout_seconds: int = 120
    cross_check: LangGraphCrossCheck = Field(default_factory=LangGraphCrossCheck)


class HealingConfig(BaseModel):
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    langgraph: LangGraphConfig = Field(default_factory=LangGraphConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    budget: HealerBudget = Field(default_factory=HealerBudget)
    # After applying a fix, watch for ``verify_window_seconds`` for the
    # same detector to re-fire. If it does, the fix is a regression.
    verify_window_seconds: int = 600
    # What to do when a fix regresses.
    regression_action: Literal["revert", "escalate", "ignore"] = "revert"
    # When ``state.restarts`` reaches this threshold, the next detection
    # SKIPS the YAML rule healer and goes directly to Claude — the
    # agentic flow is the main fix path, rules are the cheap fast
    # lane for known transients. ``None`` →
    # ``max(1, process.restart_policy.max_restarts // 5)`` (resolved
    # at runtime; default = 2 unverified restarts).
    escalate_to_claude_after: int | None = None
    # If a rule-based fix regresses (same detector re-fires inside the
    # verify window), force Claude on the *next* attempt regardless of
    # the restart counter. Rules failed; bring in the agent. Set False
    # to keep cycling through rule attempts (not recommended).
    escalate_on_rule_regression: bool = True


class NotifierSpec(BaseModel):
    kind: Literal["log", "slack_outbox", "discord_outbox", "webhook"]
    # *_outbox: outbox file destination + chat identifiers
    outbox_path: str = ".autosentry/slack_outbox.jsonl"
    channel: str | None = None
    thread_key: str | None = None
    # webhook
    url: str | None = None


class VaultNarrativeConfig(BaseModel):
    """LLM-generated prose narratives for first-occurrence vault events.

    When enabled, autosentry fires a single LLM call per "first
    occurrence" of a significant event (first pattern threshold cross,
    first regression for an incident, first exhaustion of a run) and
    drops the resulting paragraph into the relevant note's
    ``## Narrative`` section. Dedup is on by default — re-firing
    events use the templated narrative without an additional LLM call.

    Reuses :mod:`autosentry.healers.langgraph_providers` for provider
    wiring (Anthropic / OpenAI / Google with BYO API key).

    Off by default to avoid surprise billing. Use ``dispatch.mode:
    session`` for the free Claude-Code-subscription path; this
    config is for headless runs that want richer vault notes.
    """

    enabled: bool = False
    provider: Literal["anthropic", "openai", "google"] = "anthropic"
    model: str = "claude-haiku-4-5"
    # Per-LLM-call timeout for the narrator. Short — narratives are
    # decorative; they shouldn't block the monitor.
    request_timeout_seconds: int = 30


class VaultConfig(BaseModel):
    """Obsidian-compatible markdown vault of significant supervisor events.

    Inspired by claude-obsidian. The monitor writes human-readable
    summaries to ``.autosentry/vault/`` as wikilinked markdown notes
    so the operator can browse the supervisor's history as a graph —
    runs branch into child restarts; incidents branch into attempt
    chains; recurring failures aggregate into pattern notes that link
    back to every incident that shares the trace.

    The vault is *derived* from incidents + the attempts ledger — it
    doesn't store anything that isn't already on disk somewhere. Open
    ``.autosentry/vault/`` in Obsidian directly; v0.12.0 also renders
    it under ``autosentry web``.
    """

    enabled: bool = True
    path: str = ".autosentry/vault"
    # Number of incidents with the same detector + similar message
    # before autosentry creates a ``patterns/<slug>.md`` aggregator
    # note. Lower for tight feedback (every detector quickly gets a
    # pattern note); higher to wait for actual recurrence.
    pattern_threshold: int = 3
    # Maximum Levenshtein distance (as a fraction of the longer
    # message length) for two messages to count as "similar". 0.3
    # tolerates timestamps and small numeric drift; 0.1 is strict.
    similarity_threshold: float = 0.3
    # LLM narratives for first-occurrence events. Off by default.
    narratives: VaultNarrativeConfig = Field(default_factory=VaultNarrativeConfig)


class AutoSentryConfig(BaseModel):
    process: ProcessConfig
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    vault: VaultConfig = Field(default_factory=VaultConfig)
    config_snapshots: list[str] = Field(default_factory=list)
    source_explode: SourceExplodeConfig = Field(default_factory=SourceExplodeConfig)
    detectors: list[DetectorSpec] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    healing: HealingConfig = Field(default_factory=HealingConfig)
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    notifiers: list[NotifierSpec] = Field(default_factory=lambda: [NotifierSpec(kind="log")])
    state_path: str = ".autosentry/state.json"
    incidents_dir: str = ".autosentry/incidents"

    # Path the config was loaded from, set by ``load_config``. Not in YAML.
    config_path: Annotated[Path | None, Field(exclude=True)] = None

    def resolve(self, rel: str | Path) -> Path:
        """Resolve a path relative to the project root.

        All config paths — ``state_path``, ``incidents_dir``,
        ``log_dir``, ``config_snapshots``, and ``resolve(".")`` for the
        healer's repo root — are anchored on the project root, *not* the
        config file's own directory. That way the config can sit inside
        ``.autosentry/`` without those paths double-nesting into
        ``.autosentry/.autosentry/`` or snapshots resolving under
        ``.autosentry/`` instead of the repo.
        """
        p = Path(rel)
        if p.is_absolute():
            return p
        return (self._project_root() / p).resolve()

    def _project_root(self) -> Path:
        """The directory relative paths resolve against.

        When the config lives at ``<root>/.autosentry/autosentry.yaml``
        the project root is ``<root>``; for a custom ``--config`` path
        (or the legacy root-level config) it's the file's own directory.
        Falls back to the cwd when the config wasn't loaded from disk.
        """
        if self.config_path is None:
            return Path.cwd()
        parent = self.config_path.parent
        if parent.name == ".autosentry":
            return parent.parent
        return parent


_ENV_REF = re.compile(r"\$\{?([A-Z_][A-Z0-9_]*)\}?")


def _interpolate_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    return value


def _legacy_fallback(path: Path) -> Path | None:
    """Return the pre-0.8 root-level config if ``path`` points at the new
    ``.autosentry/autosentry.yaml`` slot and nothing is there yet.

    Keyed on path *shape* (``…/.autosentry/autosentry.yaml``) rather than
    equality with the default, so an explicit
    ``--config .autosentry/autosentry.yaml`` gets the same courtesy.
    """
    if path.name == "autosentry.yaml" and path.parent.name == ".autosentry":
        legacy = path.parent.parent / "autosentry.yaml"
        if legacy.exists():
            return legacy
    return None


def resolve_existing_config(path: str | Path) -> Path | None:
    """Return the config path that :func:`load_config` would read — the
    given ``path`` or its legacy fallback — or ``None`` if neither exists.

    For existence checks and phase detection (``doctor``, ``onboard``)
    that need to know *whether* a config is present without raising.
    """
    path = Path(path)
    if path.exists():
        return path
    return _legacy_fallback(path)


def load_config(path: str | Path) -> AutoSentryConfig:
    """Load and validate the config from disk.

    Looks at ``path`` (default ``.autosentry/autosentry.yaml``); if it's
    missing, falls back to a legacy root-level ``autosentry.yaml`` before
    giving up, so repos initialized before the 0.8 move keep working.
    """
    path = Path(path)
    if not path.exists():
        path = _legacy_fallback(path) or path
    path = path.resolve()
    if not path.exists():
        msg = f"config not found: {path}"
        raise FileNotFoundError(msg)
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.load(f) or {}
    data = _interpolate_env(data)
    cfg = AutoSentryConfig.model_validate(data)
    cfg.config_path = path
    _warn_on_deprecated_modes(cfg)
    return cfg


def _warn_on_deprecated_modes(cfg: AutoSentryConfig) -> None:
    """Print a one-line steering nudge for healer runtimes that have a
    better alternative in 0.10.0+.

    Currently fires only for ``healing.claude.mode: subprocess`` —
    ``claude --print`` is programmatically billed against the user's
    Anthropic account, but Claude Code users get the same diagnosis
    free via the session-dispatch path, and headless users get a
    multi-step diagnosis (and provider choice) via LangGraph. The
    legacy path stays functional; this is just a nudge.
    """
    if cfg.healing.claude.mode == "subprocess":
        # Lazy import to avoid pulling logger setup into config-load
        # for processes that don't run the monitor (CLI helpers).
        from autosentry.logger import log

        log().info(
            "healing.claude.mode: subprocess is superseded — "
            "Claude Code users should use dispatch.mode: session "
            "(free under Claude Code subscription); headless users should use "
            "healing.claude.mode: langgraph (multi-step diagnosis, BYO provider key). "
            "Subprocess mode still works."
        )
