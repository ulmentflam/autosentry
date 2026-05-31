"""LangGraph-powered headless healer.

The agentic, provider-portable counterpart to the
``claude --print`` subprocess healer. Use this when:

- You're running autosentry headless (CI / k8s / a service box) so
  there's no interactive Claude Code session to do dispatch.
- You want a multi-step diagnosis with optional second-model
  cross-check instead of a single-shot prompt.
- You want to choose your provider (Anthropic / OpenAI / Google)
  rather than be tied to ``claude``.

**Billing note.** Like ``claude --print``, every fire of this healer
costs real money against your provider key. The free path for Claude
Code users is :class:`autosentry.config.DispatchConfig` ``mode:
session`` — the ``/autosentry`` skill dispatches with subagents that
run under your Claude Code subscription.

The graph implementation lives in
:mod:`autosentry.healers.langgraph_graph`; this module is the thin
healer adapter that the Monitor's dispatch site sees.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autosentry.config import AutoSentryConfig, LangGraphConfig
from autosentry.detectors.base import Detection
from autosentry.healers.base import HealerOutcome
from autosentry.healers.langgraph_providers import ProviderConfigError
from autosentry.logger import log


class LangGraphHealer:
    """Headless multi-step LLM healer using LangGraph.

    Implements the same ``attempt`` interface as
    :class:`autosentry.healers.claude.ClaudeHealer` so the Monitor can
    call either without knowing which runtime is active.
    """

    def __init__(self, cfg: AutoSentryConfig) -> None:
        self.cfg = cfg
        self.lg_cfg: LangGraphConfig = cfg.healing.langgraph

    def attempt(
        self,
        det: Detection,
        *,
        state_dict: dict[str, Any],
        last_incident_dir: Path | None,
    ) -> HealerOutcome | None:
        if not self.lg_cfg.enabled:
            return None
        if last_incident_dir is None:
            log().info(
                "LangGraph healer skipped — no incident folder yet (Monitor calls us "
                "after writing the incident, but the path didn't propagate)."
            )
            return None

        # Lazy import: keeps autosentry CLI startup fast for users
        # who never invoke the LangGraph healer. The deps themselves
        # are still installed; we just defer the module-graph hit.
        from autosentry.healers.langgraph_graph import (
            build_graph,
            initial_state_from_detection,
        )

        log().claude(
            f"LangGraph healer invoked (provider={self.lg_cfg.provider}, "
            f"model={self.lg_cfg.model}, cross_check="
            f"{'on' if self.lg_cfg.cross_check.enabled else 'off'}, "
            f"max_steps={self.lg_cfg.max_steps})"
        )
        repo_root = self.cfg.resolve(".")
        incident_id = last_incident_dir.name

        try:
            graph = build_graph(self.cfg, repo_root, last_incident_dir)
        except ProviderConfigError as e:
            log().error(f"LangGraph healer disabled: {e}")
            return None

        initial = initial_state_from_detection(
            det,
            incident_id=incident_id,
            incident_folder=last_incident_dir,
            repo_root=repo_root,
        )
        try:
            final_state = graph.invoke(initial)
        except ProviderConfigError as e:
            log().error(f"LangGraph healer aborted: {e}")
            return None
        except Exception as e:  # noqa: BLE001
            # LangChain / LangGraph errors are diverse and provider-
            # specific (rate limit, auth, network). Log loud and let
            # the Monitor fall back to restart_policy.
            log().error(f"LangGraph healer raised: {type(e).__name__}: {e}")
            return None

        outcome = final_state.get("outcome")
        if outcome is None:
            log().recovery("LangGraph healer produced no action — falling back to restart_policy")
            return None
        return outcome
