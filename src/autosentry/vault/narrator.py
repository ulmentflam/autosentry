"""LLM-generated narratives for first-occurrence vault events.

The vault's templated ``## Narrative`` section is fine for most events.
For the *first* time a recurring pattern crosses its threshold, the
first regression on a given incident, or the first exhaustion of a run,
a paragraph of LLM-generated prose tends to add useful context — it
can name what's likely going wrong, suggest where to look, and explain
why the deterministic restart_policy gave up.

Three design constraints:

1. **First occurrence only.** Dedup state lives in ``.narrated.json``
   in the vault root. If we've already narrated a pattern slug /
   incident id / run id, subsequent same-class events use the
   templated narrative.

2. **Single LLM call, no graph.** This is decorative prose, not a
   healer decision. We reuse :func:`get_chat_model` from
   :mod:`autosentry.healers.langgraph_providers` but the agent flow
   (tools, structured output, cross-check) doesn't apply. One
   prompt, one response, drop it into the note.

3. **Best-effort.** A narrator failure (rate limit, network blip,
   bad API key) must not block the monitor or the vault. We log
   the error and the note keeps its templated narrative.

Off by default. Enable via ``vault.narratives.enabled: true`` plus a
matching provider API key in the environment.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from autosentry.logger import log

if TYPE_CHECKING:
    from autosentry.config import VaultNarrativeConfig


_NARRATED_SIDECAR = ".narrated.json"


@dataclass
class _NarratedState:
    """On-disk record of which events we've already narrated.

    Keyed by event class so a pattern slug doesn't collide with an
    incident id with the same value. JSON round-trips trivially.
    """

    patterns: set[str]
    regressions: set[str]
    exhaustions: set[str]

    @classmethod
    def empty(cls) -> _NarratedState:
        return cls(patterns=set(), regressions=set(), exhaustions=set())

    @classmethod
    def load(cls, path: Path) -> _NarratedState:
        if not path.exists():
            return cls.empty()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls.empty()
        return cls(
            patterns=set(data.get("patterns") or []),
            regressions=set(data.get("regressions") or []),
            exhaustions=set(data.get("exhaustions") or []),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "patterns": sorted(self.patterns),
            "regressions": sorted(self.regressions),
            "exhaustions": sorted(self.exhaustions),
        }
        try:
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as e:
            log().error(f"vault narrator: failed to write {path}: {e}")


class Narrator:
    """Single-LLM-call prose generator for first-occurrence events.

    The narrator is intentionally minimal — it knows how to call one
    chat model and write the result into a vault note's narrative
    section. Everything else (when to fire, dedup) is the caller's
    responsibility.
    """

    def __init__(self, cfg: VaultNarrativeConfig, vault_root: Path) -> None:
        self.cfg = cfg
        self.vault_root = vault_root
        self._state_path = vault_root / _NARRATED_SIDECAR
        self._state = _NarratedState.load(self._state_path)
        self._lock = threading.Lock()

    # ----- predicates -------------------------------------------------------

    def enabled(self) -> bool:
        """True iff narratives are configured AND the provider key is
        present in the environment. Avoids firing a doomed LLM call."""
        if not self.cfg.enabled:
            return False
        from autosentry.healers.langgraph_providers import PROVIDER_API_KEY_ENV

        env = PROVIDER_API_KEY_ENV[self.cfg.provider]
        return bool(os.environ.get(env))

    def should_narrate_pattern(self, slug: str) -> bool:
        return self.enabled() and slug not in self._state.patterns

    def should_narrate_regression(self, incident_id: str) -> bool:
        return self.enabled() and incident_id not in self._state.regressions

    def should_narrate_exhaustion(self, run_id: str) -> bool:
        return self.enabled() and run_id not in self._state.exhaustions

    # ----- the three public hooks ------------------------------------------

    def narrate_pattern(
        self,
        *,
        slug: str,
        detector: str,
        representative_message: str,
        incident_count: int,
    ) -> str | None:
        """Generate prose for a newly-promoted pattern. Returns the
        narrative or ``None`` if narrator is disabled / failed.
        Side-effect: records ``slug`` as narrated."""
        if not self.should_narrate_pattern(slug):
            return None
        prompt = (
            f"autosentry detected a recurring failure pattern.\n\n"
            f"Detector: {detector}\n"
            f"Representative message: {representative_message}\n"
            f"Incidents seen so far: {incident_count}\n\n"
            "Write a single paragraph (≤120 words) for an operator's vault note. "
            "Explain in plain English what's likely happening, what a human "
            "should look at first, and whether autosentry's deterministic "
            "rules are usually the right tool here or whether the LLM healer "
            "should take over. Don't repeat the literal message; interpret it. "
            "No headers, no lists, no markdown — just a paragraph."
        )
        return self._narrate_and_record(prompt, "patterns", slug)

    def narrate_regression(
        self,
        *,
        incident_id: str,
        detector: str,
        original_attempt: int,
    ) -> str | None:
        if not self.should_narrate_regression(incident_id):
            return None
        prompt = (
            f"autosentry applied a fix for an incident, but the same detector "
            f"re-fired inside the verify window — the fix didn't stick.\n\n"
            f"Incident: {incident_id}\n"
            f"Detector: {detector}\n"
            f"Original attempt: #{original_attempt}\n\n"
            "Write a single paragraph (≤100 words) for an operator's vault "
            "note. Explain plainly: what could cause a fix to revert this "
            "quickly, what kind of fix the LLM healer should propose on the "
            "next attempt, and any pattern the operator should be on the "
            "lookout for. No headers, no lists, no markdown — just a paragraph."
        )
        return self._narrate_and_record(prompt, "regressions", incident_id)

    def narrate_exhaustion(
        self,
        *,
        run_id: str,
        final_restart_count: int,
        max_restarts: int,
        final_detector: str | None,
    ) -> str | None:
        if not self.should_narrate_exhaustion(run_id):
            return None
        det = final_detector or "(unknown)"
        prompt = (
            f"autosentry's supervisor session ran out of restart budget "
            f"({final_restart_count}/{max_restarts}) and stopped trying. The "
            f"last unresolved detector was: {det}\n\n"
            "Write a single paragraph (≤100 words) for an operator's vault "
            "note. Explain plainly: what does it mean that a service-manager "
            "should now take over, what an operator should investigate before "
            "relaunching, and whether tuning max_restarts upward is the right "
            "next step. No headers, no lists, no markdown — just a paragraph."
        )
        return self._narrate_and_record(prompt, "exhaustions", run_id)

    # ----- internals --------------------------------------------------------

    def _narrate_and_record(self, prompt: str, kind: str, key: str) -> str | None:
        """Run the LLM call, record the key as narrated, return the
        prose. Best-effort: on failure, log and return ``None``."""
        from autosentry.healers.langgraph_providers import (
            ProviderConfigError,
            get_chat_model,
        )

        try:
            model = get_chat_model(
                provider=self.cfg.provider,
                model=self.cfg.model,
                temperature=0.3,
                request_timeout_seconds=self.cfg.request_timeout_seconds,
            )
        except ProviderConfigError as e:
            log().error(f"vault narrator: {e}")
            return None
        try:
            response = model.invoke(prompt)
            content = response.content if hasattr(response, "content") else str(response)
            if isinstance(content, list):
                # Some providers return content as a list of blocks; join the text bits.
                content = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b) for b in content
                )
            content = content.strip()
        except Exception as e:  # noqa: BLE001
            log().error(f"vault narrator: LLM call failed: {type(e).__name__}: {e}")
            return None
        if not content:
            return None
        with self._lock:
            getattr(self._state, kind).add(key)
            self._state.save(self._state_path)
        log().info(f"vault narrator: wrote {kind[:-1]} narrative for {key}")
        return content
