"""Tests for the LLM vault narrator (0.12.0).

Strategy: patch ``get_chat_model`` from
:mod:`autosentry.healers.langgraph_providers` to return a fake model
whose ``.invoke()`` returns a canned string. Real LLM calls never
happen in CI.

Coverage:

- Narrator dedup: same event class + same key fires only once.
- Disabled state: no provider key, narrator.enabled() is False,
  no LLM call.
- Note rewrite: ``VaultStore.replace_narrative`` swaps the templated
  paragraph for the LLM prose.
- Monitor wiring: the first pattern threshold cross triggers the
  narrator; subsequent same-pattern events don't re-narrate.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from autosentry.config import VaultNarrativeConfig
from autosentry.vault import VaultStore
from autosentry.vault.narrator import Narrator


class _FakeModel:
    """Minimal stand-in for a LangChain chat model — just needs ``.invoke``
    that returns an object with a ``.content`` attribute."""

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.calls = 0

    def invoke(self, _prompt):  # type: ignore[no-untyped-def]
        self.calls += 1
        return MagicMock(content=self.response_text)


def _narr_cfg(enabled: bool = True) -> VaultNarrativeConfig:
    return VaultNarrativeConfig(enabled=enabled, provider="anthropic", model="test-model")


def test_narrator_disabled_without_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    n = Narrator(_narr_cfg(enabled=True), tmp_path / "vault")
    assert n.enabled() is False
    assert (
        n.narrate_pattern(slug="x", detector="oom", representative_message="m", incident_count=3)
        is None
    )


def test_narrator_disabled_by_config(tmp_path: Path) -> None:
    n = Narrator(_narr_cfg(enabled=False), tmp_path / "vault")
    assert n.enabled() is False


def test_narrator_fires_once_per_pattern(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    n = Narrator(_narr_cfg(), tmp_path / "vault")
    model = _FakeModel("This is the LLM-generated paragraph.")
    with patch("autosentry.healers.langgraph_providers.get_chat_model", return_value=model):
        first = n.narrate_pattern(
            slug="oom-cuda",
            detector="oom",
            representative_message="OutOfMemoryError",
            incident_count=3,
        )
        second = n.narrate_pattern(
            slug="oom-cuda",
            detector="oom",
            representative_message="OutOfMemoryError",
            incident_count=4,
        )
    assert first == "This is the LLM-generated paragraph."
    assert second is None, "second call should be deduped"
    assert model.calls == 1


def test_narrator_persists_dedup_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After narrating once, a fresh ``Narrator`` instance reading from
    disk should still skip that event class+key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    vault_root = tmp_path / "vault"
    n = Narrator(_narr_cfg(), vault_root)
    model = _FakeModel("first occurrence")
    with patch("autosentry.healers.langgraph_providers.get_chat_model", return_value=model):
        n.narrate_regression(incident_id="inc-1", detector="tb", original_attempt=1)

    n2 = Narrator(_narr_cfg(), vault_root)
    assert n2.should_narrate_regression("inc-1") is False
    assert n2.should_narrate_regression("inc-2") is True


def test_narrator_handles_llm_failure_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rate-limit / network error should log and return ``None`` —
    not crash."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    n = Narrator(_narr_cfg(), tmp_path / "vault")
    raising_model = MagicMock()
    raising_model.invoke.side_effect = RuntimeError("rate limited")
    with patch(
        "autosentry.healers.langgraph_providers.get_chat_model",
        return_value=raising_model,
    ):
        out = n.narrate_exhaustion(
            run_id="run-1",
            final_restart_count=5,
            max_restarts=5,
            final_detector="oom",
        )
    assert out is None


def test_replace_narrative_swaps_section_body(tmp_path: Path) -> None:
    """The vault store rewrites the ``## Narrative`` section while
    leaving frontmatter + later sections intact."""
    vault = VaultStore(tmp_path / "vault")
    vault.record_run_start(
        run_id="run-1",
        supervisor_kind="local",
        command=["true"],
        started_at="2026-05-31T00:00:00+00:00",
        cwd=".",
    )
    vault.record_incident(
        incident_id="inc-1",
        run_id="run-1",
        child_run_id=None,
        detector="oom",
        kind="error",
        message="OOM",
        fired_at="2026-05-31T00:01:00+00:00",
        trace_hash=None,
        pattern_slug=None,
        incident_folder_rel=".autosentry/incidents/inc-1",
    )
    note_path = vault.root / "incidents" / "inc-1.md"
    original = note_path.read_text()
    assert "## Narrative" in original
    assert "fired at" in original  # templated narrative content

    vault.replace_narrative(note_path, "An LLM wrote this paragraph instead.")
    new = note_path.read_text()
    assert "## Narrative" in new
    assert "An LLM wrote this paragraph instead." in new
    # Frontmatter survives, later sections survive.
    assert "kind: incident" in new
    assert "## Attempts" in new


def test_monitor_invokes_narrator_on_first_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end-lite: when the pattern threshold crosses, the
    narrator fires once. Subsequent same-pattern incidents don't."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        """
process:
  kind: local
  command: ["true"]
detectors:
  - kind: pattern
    name: oom
    regex: "OOM"
rules: []
healing:
  claude:
    enabled: false
vault:
  enabled: true
  pattern_threshold: 2
  narratives:
    enabled: true
    provider: anthropic
    model: test
state_path: ".autosentry/state.json"
incidents_dir: ".autosentry/incidents"
"""
    )
    from autosentry.config import load_config
    from autosentry.detectors.base import Detection
    from autosentry.monitor import Monitor

    cfg = load_config(cfg_path)
    monitor = Monitor(cfg)
    monitor.supervisor = MagicMock()
    monitor.supervisor.status.return_value = MagicMock(running=True, pid=1, exit_code=None)
    monitor._notify = MagicMock()
    monitor._vault_run_id = "run-test"
    monitor.vault.record_run_start(  # type: ignore[union-attr]
        run_id="run-test",
        supervisor_kind="local",
        command=["true"],
        started_at="2026-05-31T00:00:00+00:00",
        cwd=str(tmp_path),
    )

    model = _FakeModel("This is the pattern narrative.")
    import time as _time

    with patch("autosentry.healers.langgraph_providers.get_chat_model", return_value=model):
        for _ in range(2):
            monitor._fire_detection(Detection(kind="error", detector="oom", message="OOM"))
            _time.sleep(1.05)  # distinct second-resolution slugs
        # Third fire of the same pattern shouldn't re-narrate.
        _time.sleep(0.1)
        monitor._fire_detection(Detection(kind="error", detector="oom", message="OOM"))

    assert model.calls == 1, f"expected 1 LLM call, got {model.calls}"
    # Pattern note should carry the LLM prose.
    pattern_notes = list((tmp_path / ".autosentry" / "vault" / "patterns").glob("*.md"))
    assert pattern_notes, "expected a pattern note"
    body = pattern_notes[0].read_text()
    assert "This is the pattern narrative." in body
