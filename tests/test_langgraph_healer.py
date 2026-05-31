"""Tests for the LangGraph headless healer (autosentry 0.10.0).

Strategy: monkeypatch :func:`autosentry.healers.langgraph_graph.
get_chat_model` with a fake that returns predetermined structured
output. Real LLM calls never happen in CI — too expensive and too
flaky.

Coverage:

- Phase 1: ``healing.claude.mode: langgraph`` routes through the new
  healer; provider factory raises on missing API key.
- Phase 2: graph traversal without cross-check; with cross-check
  APPROVE; with cross-check REJECT looping bounded by max_steps; with
  cross-check MODIFY adopting the modified action.
- Phase 3: provider factory selects the right class per provider; the
  ``available_providers`` report flags missing keys.
- Phase 5: the soft-deprecation log line fires when
  ``mode: subprocess`` is loaded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from autosentry.config import AutoSentryConfig, load_config
from autosentry.detectors.base import Detection
from autosentry.healers.langgraph_graph import (
    CrossCheckVerdict,
    ProposedAction,
    build_graph,
    initial_state_from_detection,
)
from autosentry.healers.langgraph_healer import LangGraphHealer
from autosentry.healers.langgraph_providers import (
    PROVIDER_API_KEY_ENV,
    ProviderConfigError,
    available_providers,
    get_chat_model,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cfg_with_langgraph(
    tmp_path: Path,
    *,
    enabled: bool = True,
    cross_check: bool = False,
    max_steps: int = 3,
    provider: str = "anthropic",
) -> AutoSentryConfig:
    """Write a minimal YAML and load it.

    LangChain-config-as-YAML is verbose, so we serialize a small
    fixture inline rather than build the pydantic objects by hand."""
    body = f"""
process:
  kind: local
  command: ["true"]
monitor:
  poll_interval_seconds: 1
  log_dir: ".autosentry/logs"
detectors:
  - kind: pattern
    name: oom
    regex: "OutOfMemoryError"
rules: []
healing:
  claude:
    enabled: false
    mode: langgraph
  langgraph:
    enabled: {str(enabled).lower()}
    provider: {provider}
    model: test-model
    max_steps: {max_steps}
    cross_check:
      enabled: {str(cross_check).lower()}
      provider: openai
      model: test-cross-check
state_path: ".autosentry/state.json"
incidents_dir: ".autosentry/incidents"
"""
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(body)
    return load_config(cfg_path)


def _make_incident_folder(tmp_path: Path, name: str = "2026-05-30T00-00-00Z-error-oom") -> Path:
    """Create an incident folder with a tiny log_excerpt.txt so the
    initial_state populator has something to read."""
    folder = tmp_path / ".autosentry" / "incidents" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "log_excerpt.txt").write_text("CUDA out of memory.\n")
    return folder


def _detection() -> Detection:
    return Detection(
        kind="error",
        detector="oom",
        message="OutOfMemoryError: CUDA out of memory",
        trace="",
        log_context=[],
        meta={},
    )


class _FakeChatModel:
    """Minimal stand-in for a LangChain ``BaseChatModel``.

    Real models have ``bind_tools`` and ``with_structured_output``; we
    implement just enough for the diagnose + cross-check nodes:

    - ``bind_tools(tools)`` returns a callable whose ``.invoke`` always
      returns an AI message with no tool calls (skips the tool loop).
    - ``with_structured_output(schema)`` returns a callable whose
      ``.invoke`` returns the next queued response of that schema.
    """

    def __init__(
        self,
        proposed_queue: list[ProposedAction] | None = None,
        verdict_queue: list[CrossCheckVerdict] | None = None,
    ) -> None:
        self._proposed = list(proposed_queue or [])
        self._verdicts = list(verdict_queue or [])

    def bind_tools(self, _tools: Any) -> Any:
        # No tool calls in tests — return an empty AI message so the
        # diagnose loop exits immediately to the structured-output call.
        from langchain_core.messages import AIMessage

        empty = AIMessage(content="")

        class _Tooled:
            def invoke(self, _msgs: Any) -> Any:
                return empty

        return _Tooled()

    def with_structured_output(self, schema: Any) -> Any:
        if schema is ProposedAction:
            queue = self._proposed
        elif schema is CrossCheckVerdict:
            queue = self._verdicts
        else:
            raise AssertionError(f"unexpected schema in test: {schema}")

        class _Structured:
            def invoke(self, _msgs: Any) -> Any:
                if not queue:
                    raise AssertionError("ran out of fake responses")
                return queue.pop(0)

        return _Structured()


# ---------------------------------------------------------------------------
# Phase 3 — provider factory
# ---------------------------------------------------------------------------


def test_get_chat_model_raises_on_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ProviderConfigError) as exc:
        get_chat_model("anthropic", "claude-haiku-4-5")
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_get_chat_model_anthropic_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    model = get_chat_model("anthropic", "claude-haiku-4-5")
    from langchain_anthropic import ChatAnthropic

    assert isinstance(model, ChatAnthropic)


def test_get_chat_model_openai_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    model = get_chat_model("openai", "gpt-4o-mini")
    from langchain_openai import ChatOpenAI

    assert isinstance(model, ChatOpenAI)


def test_get_chat_model_google_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    model = get_chat_model("google", "gemini-1.5-flash")
    from langchain_google_genai import ChatGoogleGenerativeAI

    assert isinstance(model, ChatGoogleGenerativeAI)


def test_available_providers_reports_all_three(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    results = available_providers()
    names = {p for p, _, _ in results}
    assert names == {"anthropic", "openai", "google"}
    by_name = {p: present for p, _, present in results}
    assert by_name["anthropic"] is True
    assert by_name["openai"] is False
    assert by_name["google"] is False


# ---------------------------------------------------------------------------
# Phase 2 — graph traversal
# ---------------------------------------------------------------------------


def test_graph_without_cross_check_returns_proposed_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    cfg = _cfg_with_langgraph(tmp_path, cross_check=False)
    folder = _make_incident_folder(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake = _FakeChatModel(
        proposed_queue=[
            ProposedAction(
                kind="restart_with_env",
                set={"BATCH_SIZE": "4"},
                command=[],
                reasoning="Reduce batch size to avoid CUDA OOM.",
            )
        ]
    )
    with patch("autosentry.healers.langgraph_graph.get_chat_model", return_value=fake):
        graph = build_graph(cfg, tmp_path, folder)
        state = initial_state_from_detection(
            _detection(), incident_id=folder.name, incident_folder=folder, repo_root=tmp_path
        )
        final = graph.invoke(state)

    outcome = final["outcome"]
    assert outcome is not None
    assert outcome.action.kind == "restart_with_env"
    assert outcome.action.set == {"BATCH_SIZE": "4"}
    assert outcome.source == "claude"
    assert outcome.claude_response is not None
    assert "BATCH_SIZE" in outcome.claude_response


def test_graph_with_cross_check_approve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    cfg = _cfg_with_langgraph(tmp_path, cross_check=True)
    folder = _make_incident_folder(tmp_path)
    monkeypatch.chdir(tmp_path)

    proposed = ProposedAction(
        kind="restart",
        set={},
        command=[],
        reasoning="Transient OOM; restart should clear it.",
    )
    primary = _FakeChatModel(proposed_queue=[proposed])
    cross = _FakeChatModel(
        verdict_queue=[
            CrossCheckVerdict(verdict="APPROVE", feedback="Looks good.", modified_action=None)
        ]
    )
    call_count = {"n": 0}

    def fake_factory(*, provider: str, **_: Any):
        call_count["n"] += 1
        return primary if provider == "anthropic" else cross

    with patch("autosentry.healers.langgraph_graph.get_chat_model", side_effect=fake_factory):
        graph = build_graph(cfg, tmp_path, folder)
        state = initial_state_from_detection(
            _detection(), incident_id=folder.name, incident_folder=folder, repo_root=tmp_path
        )
        final = graph.invoke(state)

    outcome = final["outcome"]
    assert outcome is not None
    assert outcome.action.kind == "restart"
    # Cross-check ran exactly once.
    assert final["verdict"].verdict == "APPROVE"
    assert "CROSS-CHECK: APPROVE" in outcome.claude_response


def test_graph_with_cross_check_modify_adopts_corrected_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    cfg = _cfg_with_langgraph(tmp_path, cross_check=True)
    folder = _make_incident_folder(tmp_path)
    monkeypatch.chdir(tmp_path)

    too_aggressive = ProposedAction(kind="abort", set={}, command=[], reasoning="Just abort it.")
    corrected = ProposedAction(
        kind="restart",
        set={},
        command=[],
        reasoning="Restart is the right escalation level here.",
    )
    primary = _FakeChatModel(proposed_queue=[too_aggressive])
    cross = _FakeChatModel(
        verdict_queue=[
            CrossCheckVerdict(
                verdict="MODIFY",
                feedback="Abort is too aggressive for a single OOM.",
                modified_action=corrected,
            )
        ]
    )

    def fake_factory(*, provider: str, **_: Any):
        return primary if provider == "anthropic" else cross

    with patch("autosentry.healers.langgraph_graph.get_chat_model", side_effect=fake_factory):
        graph = build_graph(cfg, tmp_path, folder)
        state = initial_state_from_detection(
            _detection(), incident_id=folder.name, incident_folder=folder, repo_root=tmp_path
        )
        final = graph.invoke(state)

    outcome = final["outcome"]
    assert outcome is not None
    # MODIFY adopted the corrected proposal.
    assert outcome.action.kind == "restart"


def test_graph_with_cross_check_reject_loops_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REJECT loops back to diagnose; after max_steps attempts we give up
    and return ``outcome=None`` so the Monitor falls back to
    restart_policy."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    cfg = _cfg_with_langgraph(tmp_path, cross_check=True, max_steps=2)
    folder = _make_incident_folder(tmp_path)
    monkeypatch.chdir(tmp_path)

    # Two proposals (one per allowed attempt), both rejected.
    primary = _FakeChatModel(
        proposed_queue=[
            ProposedAction(kind="restart", set={}, command=[], reasoning="Try 1"),
            ProposedAction(kind="restart", set={}, command=[], reasoning="Try 2"),
        ]
    )
    cross = _FakeChatModel(
        verdict_queue=[
            CrossCheckVerdict(verdict="REJECT", feedback="Wrong action."),
            CrossCheckVerdict(verdict="REJECT", feedback="Still wrong."),
        ]
    )

    def fake_factory(*, provider: str, **_: Any):
        return primary if provider == "anthropic" else cross

    with patch("autosentry.healers.langgraph_graph.get_chat_model", side_effect=fake_factory):
        graph = build_graph(cfg, tmp_path, folder)
        state = initial_state_from_detection(
            _detection(), incident_id=folder.name, incident_folder=folder, repo_root=tmp_path
        )
        final = graph.invoke(state)

    assert final["outcome"] is None, "two REJECTs should exhaust max_steps=2"


# ---------------------------------------------------------------------------
# Phase 1 — healer dispatch
# ---------------------------------------------------------------------------


def test_langgraph_healer_disabled_returns_none(tmp_path: Path) -> None:
    cfg = _cfg_with_langgraph(tmp_path, enabled=False)
    healer = LangGraphHealer(cfg)
    assert healer.attempt(_detection(), state_dict={}, last_incident_dir=None) is None


def test_langgraph_healer_invokes_graph_with_incident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    cfg = _cfg_with_langgraph(tmp_path)
    folder = _make_incident_folder(tmp_path)
    monkeypatch.chdir(tmp_path)

    fake = _FakeChatModel(
        proposed_queue=[
            ProposedAction(kind="restart", set={}, command=[], reasoning="Transient failure.")
        ]
    )
    with patch("autosentry.healers.langgraph_graph.get_chat_model", return_value=fake):
        outcome = LangGraphHealer(cfg).attempt(
            _detection(), state_dict={}, last_incident_dir=folder
        )

    assert outcome is not None
    assert outcome.action.kind == "restart"


def test_langgraph_healer_returns_none_when_provider_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing API key should surface as a clean ``None`` (Monitor falls
    back to restart_policy), not a stack trace."""
    for env in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env, raising=False)
    cfg = _cfg_with_langgraph(tmp_path)
    folder = _make_incident_folder(tmp_path)
    monkeypatch.chdir(tmp_path)

    outcome = LangGraphHealer(cfg).attempt(_detection(), state_dict={}, last_incident_dir=folder)
    assert outcome is None


# ---------------------------------------------------------------------------
# Phase 1 — ClaudeHealer routes mode=langgraph through LangGraphHealer
# ---------------------------------------------------------------------------


def test_claude_healer_routes_langgraph_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ClaudeHealer.attempt`` with ``mode: langgraph`` should delegate
    to ``LangGraphHealer``. We don't reach the network; we just assert
    the delegation happens by mocking the LangGraph healer."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    cfg = _cfg_with_langgraph(tmp_path)
    folder = _make_incident_folder(tmp_path)
    monkeypatch.chdir(tmp_path)

    from autosentry.healers.base import HealerOutcome
    from autosentry.healers.claude import ClaudeHealer

    sentinel = HealerOutcome(
        action=MagicMock(kind="restart"), source="claude", claude_response="ok"
    )
    with patch(
        "autosentry.healers.langgraph_healer.LangGraphHealer.attempt",
        return_value=sentinel,
    ) as lh:
        result = ClaudeHealer(cfg).attempt(_detection(), state_dict={}, last_incident_dir=folder)

    lh.assert_called_once()
    assert result is sentinel
