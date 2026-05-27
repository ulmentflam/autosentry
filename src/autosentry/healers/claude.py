"""Claude-driven recovery healer with two modes.

Invoked when no YAML rule matches a detection. Two ways to reach
Claude:

- **subprocess** (the original mode): spawns ``claude --print``, pipes
  in the prompt, captures stdout. Works headless; no human session
  required. Use this in CI, k8s, anywhere without an interactive
  Claude Code session.

- **interactive**: writes a recovery request file with YAML
  frontmatter describing which subagent to spawn, then blocks on a
  response file. The ``/autosentry`` skill running in the user's
  open Claude Code session sees the request, spawns the right
  subagent type via the Task tool, and writes the response with
  ``autosentry healer respond``. Keeps the operator's main session
  clean and lets a specialized agent do the diagnosis.

Mode resolution (``ClaudeConfig.mode``):

- ``auto``: ``interactive`` when the skill is installed, otherwise
  ``subprocess`` when ``claude`` is on PATH, otherwise the healer
  is skipped (rule-only behavior).
- ``subprocess`` / ``interactive``: forced.

Whatever mode runs, the response is parsed for an explicit ``ACTION:``
block; missing block defaults to ``restart`` (assume Claude edited
files and just wants a relaunch).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from autosentry.config import AutoSentryConfig, ClaudeConfig, RuleAction, SubagentSpec
from autosentry.detectors.base import Detection
from autosentry.healers.base import HealerOutcome
from autosentry.logger import log

_ACTION_RE = re.compile(r"^ACTION:\s*(\w+)\s*$", re.MULTILINE)
_SET_LINE_RE = re.compile(r"^SET:\s*(.+?)=(.+?)\s*$", re.MULTILINE)

# Per-tool skill paths the resolver checks when figuring out whether
# interactive mode is even possible.
_SKILL_MARKERS = (
    ".claude/commands/autosentry.md",
    ".opencode/command/autosentry.md",
    ".codex/prompts/autosentry.md",
    ".gemini/commands/autosentry.toml",
    ".cursor/commands/autosentry.md",
    "AGENTS.md",
)


class ClaudeHealer:
    def __init__(self, cfg: AutoSentryConfig) -> None:
        self.cfg = cfg
        self.claude_cfg: ClaudeConfig = cfg.healing.claude

    # ----- public API -------------------------------------------------------

    def attempt(
        self,
        det: Detection,
        *,
        state_dict: dict[str, Any],
        last_incident_dir: Path | None,
    ) -> HealerOutcome | None:
        if not self._is_enabled():
            return None
        mode = self._resolve_mode()
        if mode == "disabled":
            log().info("Claude healer skipped — no usable mode (no skill installed, no CLI)")
            return None

        prompt = self._build_prompt(det, state_dict, last_incident_dir)
        repo_root = self.cfg.resolve(".")
        diff_before = _git_diff_or_empty(repo_root)

        if mode == "interactive":
            response = self._interactive_attempt(det, prompt)
        else:
            response = self._subprocess_attempt(prompt, repo_root)
        if response is None:
            return None

        diff_after = _git_diff_or_empty(repo_root)
        fix_diff = _new_diff(diff_before, diff_after)
        action = _parse_action(response)
        log().claude(
            f"Claude responded ({len(response)} chars, mode={mode}), "
            f"action={action.kind}, diff={'yes' if fix_diff else 'no'}"
        )
        return HealerOutcome(
            action=action,
            source="claude",
            claude_response=response,
            claude_diff=fix_diff if fix_diff else None,
        )

    # ----- mode resolution --------------------------------------------------

    def _is_enabled(self) -> bool:
        enabled = self.claude_cfg.enabled
        if enabled is False:
            return False
        if enabled is True:
            return True
        # "auto" — let _resolve_mode decide; we say "enabled" here and let
        # the mode resolver short-circuit to disabled if no path works.
        return True

    def _resolve_mode(self) -> str:
        """Returns one of: ``subprocess``, ``interactive``, ``disabled``."""
        configured = self.claude_cfg.mode
        if configured == "subprocess":
            return "subprocess" if self._claude_cli_present() else "disabled"
        if configured == "interactive":
            return "interactive"  # user explicitly asked; we don't gatekeep
        # auto
        if self._skill_installed():
            return "interactive"
        if self._claude_cli_present():
            return "subprocess"
        return "disabled"

    def _skill_installed(self) -> bool:
        repo_root = self.cfg.resolve(".")
        return any((repo_root / marker).exists() for marker in _SKILL_MARKERS)

    def _claude_cli_present(self) -> bool:
        binary = (self.claude_cfg.command or ["claude"])[0]
        return shutil.which(binary) is not None

    # ----- subprocess mode --------------------------------------------------

    def _subprocess_attempt(self, prompt: str, repo_root: Path) -> str | None:
        log().claude(f"Invoking Claude via subprocess (timeout={self.claude_cfg.timeout_seconds}s)")
        try:
            result = subprocess.run(  # noqa: S603
                [*self.claude_cfg.command],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.claude_cfg.timeout_seconds,
                cwd=str(repo_root),
                check=False,
            )
        except subprocess.TimeoutExpired:
            log().error("Claude invocation timed out")
            return None
        except FileNotFoundError as e:
            log().error(f"Claude CLI not found: {e}. Set healing.claude.command or disable.")
            return None
        if result.returncode != 0:
            log().error(f"Claude exited {result.returncode}: {result.stderr[:500]}")
            return None
        return result.stdout

    # ----- interactive mode -------------------------------------------------

    def _interactive_attempt(self, det: Detection, prompt: str) -> str | None:
        """Write a request file, block on the response file.

        The skill running in the user's Claude session is the dispatcher —
        it polls the request marker, spawns a subagent of the type chosen
        below, and writes the response file (typically via
        ``autosentry healer respond``).
        """
        request_path = self.cfg.resolve(self.claude_cfg.request_path)
        response_path = self.cfg.resolve(self.claude_cfg.response_path)
        request_path.parent.mkdir(parents=True, exist_ok=True)

        # Stale-response guard: if a previous run left a response file
        # behind, treat that mtime as the baseline so we don't immediately
        # consume it. (The caller would otherwise see "instant" responses
        # that aren't ours.)
        baseline_mtime = response_path.stat().st_mtime if response_path.exists() else 0.0

        subagent = self._pick_subagent(det)
        body = _format_request(
            incident_id=_latest_incident_id(self.cfg) or "unknown",
            detector=det.detector,
            subagent=subagent,
            timeout_seconds=self.claude_cfg.timeout_seconds,
            prompt=prompt,
        )
        request_path.write_text(body, encoding="utf-8")
        log().claude(
            f"Wrote {request_path} — waiting up to {self.claude_cfg.timeout_seconds}s "
            f"for the Claude session to respond (subagent type: {subagent.type})"
        )

        deadline = time.monotonic() + self.claude_cfg.timeout_seconds
        while time.monotonic() < deadline:
            if response_path.exists():
                mtime = response_path.stat().st_mtime
                if mtime > baseline_mtime:
                    text = response_path.read_text(encoding="utf-8")
                    return text
            time.sleep(1.0)
        log().error("interactive Claude healer timed out waiting for response")
        return None

    def _pick_subagent(self, det: Detection) -> SubagentSpec:
        subs = self.claude_cfg.subagents or {}
        if det.detector in subs:
            return subs[det.detector]
        if "default" in subs:
            return subs["default"]
        return SubagentSpec()  # built-in default

    # ----- shared prompt builder --------------------------------------------

    def _build_prompt(
        self,
        det: Detection,
        state_dict: dict[str, Any],
        last_incident_dir: Path | None,
    ) -> str:
        template_path = self.cfg.resolve(self.claude_cfg.prompt_template)
        if template_path.exists():
            template = template_path.read_text(encoding="utf-8")
        else:
            template = _DEFAULT_PROMPT

        snippet_blocks: list[str] = []
        if "state" in self.claude_cfg.include_in_context:
            import json

            state_block = json.dumps(state_dict, indent=2, default=str)
            snippet_blocks.append("## Current state\n\n```json\n" + state_block + "\n```\n")
        if "last_incident" in self.claude_cfg.include_in_context and last_incident_dir:
            report = last_incident_dir / "report.md"
            if report.exists():
                snippet_blocks.append(
                    f"## Incident report\n\nLocation: `{last_incident_dir}`\n\n"
                    + report.read_text(encoding="utf-8")
                )
        if "config_snapshots" in self.claude_cfg.include_in_context:
            for snap in self.cfg.config_snapshots:
                p = self.cfg.resolve(snap)
                if p.exists():
                    snippet_blocks.append(
                        f"## Config `{snap}`\n\n```\n{p.read_text(encoding='utf-8')[:8000]}\n```\n"
                    )

        return template.format(
            detector=det.detector,
            kind=det.kind,
            message=det.message,
            trace=(det.trace or "<no trace>"),
            context="\n".join(snippet_blocks),
        )


# ----- request/response helpers --------------------------------------------


def _format_request(
    *,
    incident_id: str,
    detector: str,
    subagent: SubagentSpec,
    timeout_seconds: int,
    prompt: str,
) -> str:
    """Render the recovery request markdown the skill consumes.

    YAML frontmatter so the skill can grep / parse the routing metadata
    cheaply; body is the same prompt the subprocess mode would have
    piped into ``claude --print``.
    """
    # Hand-rolled YAML to keep this dependency-free at write time. The
    # values are constrained and safe.
    fm = (
        "---\n"
        f"incident_id: {incident_id}\n"
        f"detector: {detector}\n"
        "subagent:\n"
        f"  type: {subagent.type}\n"
        f"  description: {subagent.description}\n"
        f"timeout_seconds: {timeout_seconds}\n"
        "---\n\n"
    )
    return fm + prompt


def _latest_incident_id(cfg: AutoSentryConfig) -> str | None:
    """Best-effort: find the most recent incident folder. Used as the
    incident_id when the healer doesn't know it yet (called before the
    incident store has written the folder)."""
    incidents_dir = cfg.resolve(cfg.incidents_dir)
    if not incidents_dir.exists():
        return None
    try:
        entries = sorted(
            (p for p in incidents_dir.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    return entries[0].name if entries else None


_DEFAULT_PROMPT = """\
You are autosentry's recovery agent. A long-running process being supervised
just hit an issue. Diagnose and apply a minimal, surgical fix in the repo,
then declare the recovery action you want autosentry to take.

Detection
- detector: {detector}
- kind: {kind}
- message: {message}

Trace:

```
{trace}
```

{context}

Tasks:
1. Identify the root cause. Edit files as needed (the diff will be captured).
2. End your response with an ACTION block. Examples:

   ACTION: restart_with_env
   SET: BATCH_SIZE=4

   ACTION: restart

   ACTION: abort

If you cannot diagnose with confidence, respond with `ACTION: abort` and
explain why.
"""


def _parse_action(response: str) -> RuleAction:
    m = _ACTION_RE.search(response)
    kind = m.group(1).strip() if m else "restart"
    valid = {"restart", "restart_with_env", "pause", "abort", "custom_command"}
    if kind not in valid:
        kind = "restart"
    sets = dict(_SET_LINE_RE.findall(response))
    return RuleAction(kind=kind, set=sets)  # type: ignore[arg-type]


def _git_diff_or_empty(repo_root: Path) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "diff", "--no-color"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def _new_diff(before: str, after: str) -> str:
    """Diff that appeared between two ``git diff`` snapshots."""
    if not after:
        return ""
    if before and after.startswith(before):
        return after[len(before) :]
    return after


# `os` is imported for future use (e.g. honoring env-var overrides on
# paths). Currently only ``os.environ.get`` is referenced from elsewhere in
# the project; here we just want to keep the import-graph stable for any
# future change. The next pass can re-evaluate.
_ = os
