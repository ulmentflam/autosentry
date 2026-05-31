"""LangGraph diagnosis graph for the headless healer.

Graph shape::

    prepare_context -> diagnose -> should_cross_check?
                          ^             |
                          |             |-- no  -> finalize
                          |             `-- yes -> cross_check -> should_loop?
                          |                                |
                          |                                |-- APPROVE/MODIFY -> finalize
                          `--------- REJECT (attempt < max_steps) -'

- ``prepare_context`` packs the incident folder into the message history.
- ``diagnose`` is a ReAct agent: an LLM with three repo-aware tools
  (``read_file``, ``grep_repo``, ``view_log_excerpt``). It returns a
  structured :class:`ProposedAction`.
- ``cross_check`` (optional, when ``cfg.healing.langgraph.cross_check
  .enabled``) is a single LLM call with **no tools** that grades the
  proposal as APPROVE / MODIFY / REJECT.
- REJECT loops back to ``diagnose`` up to ``max_steps`` times, with
  the cross-checker's feedback fed into the next attempt's prompt.
- ``finalize`` builds a :class:`HealerOutcome`.

The graph is provider-agnostic — every LLM call goes through
:mod:`autosentry.healers.langgraph_providers`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

from pydantic import BaseModel, Field

from autosentry.config import LangGraphConfig
from autosentry.healers.base import HealerOutcome
from autosentry.healers.langgraph_providers import get_chat_model
from autosentry.logger import log

if TYPE_CHECKING:
    from autosentry.config import AutoSentryConfig
    from autosentry.detectors.base import Detection

_ACTION_KINDS = ("restart", "restart_with_env", "pause", "abort", "custom_command")


# ---------------------------------------------------------------------------
# Structured output schemas
# ---------------------------------------------------------------------------


class ProposedAction(BaseModel):
    """The primary diagnose node's structured output.

    Mirrors :class:`autosentry.config.RuleAction` plus a free-text
    ``reasoning`` field for the incident log.
    """

    kind: Literal["restart", "restart_with_env", "pause", "abort", "custom_command"]
    set: dict[str, str] = Field(
        default_factory=dict,
        description="Env overrides for restart_with_env. Empty for other kinds.",
    )
    command: list[str] = Field(
        default_factory=list,
        description="Command argv for custom_command. Empty for other kinds.",
    )
    reasoning: str = Field(
        description="One-paragraph explanation of why this action fits this incident."
    )


class CrossCheckVerdict(BaseModel):
    """The cross-check node's structured output."""

    verdict: Literal["APPROVE", "REJECT", "MODIFY"]
    feedback: str = Field(
        description=(
            "If REJECT: what's wrong and what to try instead. "
            "If MODIFY: the specific change to make. "
            "If APPROVE: optional rationale."
        )
    )
    modified_action: ProposedAction | None = Field(
        default=None,
        description="When verdict=MODIFY, the corrected proposal. Otherwise null.",
    )


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------


class GraphState(TypedDict, total=False):
    """Per-invocation state threaded through the graph."""

    # Inputs
    incident_id: str
    incident_folder: str
    repo_root: str
    detector: str
    detection_kind: str  # "anomaly" | "error"
    detection_message: str
    detection_trace: str
    log_excerpt: str

    # Loop bookkeeping
    attempt: int  # 1-indexed; bounded by cfg.max_steps
    feedback: str | None  # carried from cross_check REJECT back to diagnose

    # Outputs
    proposed: ProposedAction | None
    verdict: CrossCheckVerdict | None
    outcome: HealerOutcome | None


# ---------------------------------------------------------------------------
# Tools (LLM-callable)
# ---------------------------------------------------------------------------


def _make_tools(repo_root: Path, incident_folder: Path):
    """Build the three repo-aware tools, bound to the current incident.

    Each tool is sandboxed: ``read_file`` rejects paths outside
    ``repo_root``; ``grep_repo`` runs ripgrep with the repo as cwd;
    ``view_log_excerpt`` reads from the incident folder only.
    """
    from langchain_core.tools import tool

    @tool
    def read_file(path: str) -> str:
        """Read a file from the repo. Use this to inspect source code,
        configs, or the incident folder's contents. Paths are relative
        to the repo root; absolute paths outside the repo are rejected.
        Output is truncated to 4000 characters."""
        p = (repo_root / path).resolve()
        try:
            p.relative_to(repo_root.resolve())
        except ValueError:
            return f"ERROR: path {path!r} is outside the repo root; refusing to read."
        if not p.exists():
            return f"ERROR: {path!r} does not exist."
        if not p.is_file():
            return f"ERROR: {path!r} is not a regular file."
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"ERROR: could not read {path!r}: {e}"
        if len(text) > 4000:
            return text[:4000] + f"\n... [truncated; full file was {len(text)} chars]"
        return text

    @tool
    def grep_repo(pattern: str) -> str:
        """Run ripgrep against the repo for ``pattern`` (regex). Returns
        the first 50 matches with file:line context. Use this to find
        where a symbol is defined or referenced."""
        try:
            result = subprocess.run(  # noqa: S603
                ["rg", "--no-heading", "--max-count=50", "--line-number", pattern],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except FileNotFoundError:
            return "ERROR: ripgrep (rg) is not on PATH."
        except subprocess.TimeoutExpired:
            return "ERROR: ripgrep timed out."
        out = result.stdout.strip()
        if not out:
            return f"(no matches for pattern {pattern!r})"
        if len(out) > 4000:
            return out[:4000] + "\n... [truncated]"
        return out

    @tool
    def view_log_excerpt() -> str:
        """Return the supervised process's recent log lines from the
        incident folder. The log excerpt is the most useful single piece
        of context for diagnosing why a detection fired."""
        log_path = incident_folder / "log_excerpt.txt"
        if not log_path.exists():
            return "(no log excerpt available for this incident)"
        text = log_path.read_text(encoding="utf-8", errors="replace")
        if len(text) > 4000:
            return text[-4000:]  # tail is more useful than head
        return text

    return [read_file, grep_repo, view_log_excerpt]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


_DIAGNOSE_SYSTEM = """You are the LangGraph healer for autosentry, a supervisor that watches
a long-running process and applies fixes when it misbehaves.

A detection has fired. Use the provided tools to read the incident folder
and the repo, then return ONE structured action that fixes the underlying
problem. Available actions:

- restart: relaunch the process unchanged. Use when the failure is transient
  (network blip, OOM that will retry, etc.).
- restart_with_env: relaunch with env overrides. Use when the fix is a config
  change (e.g. NCCL_DEBUG=INFO, BATCH_SIZE=4). Set the ``set`` dict.
- pause: stop the process without restarting. Use when continuing would make
  things worse and a human should look.
- abort: shut the supervisor down entirely. Use as a last resort.
- custom_command: run a custom command before deciding. Set ``command`` to
  the argv list.

Always include a one-paragraph ``reasoning`` field explaining the fix.
Prefer the least invasive action that addresses the actual cause."""

_CROSS_CHECK_SYSTEM = """You are reviewing a proposed autosentry fix before it gets applied.
Reply with one of:

- APPROVE: the proposal correctly addresses the failure.
- MODIFY: the proposal is close but needs a tweak. Include ``modified_action``
  with the corrected version.
- REJECT: the proposal is wrong. Include ``feedback`` describing what's
  wrong and what to try instead.

Be strict — autosentry will apply this action against a live process. If
the reasoning is hand-wavy or the action is more aggressive than needed,
push back."""


def _format_detection_prompt(state: GraphState) -> str:
    feedback_block = ""
    if state.get("feedback"):
        feedback_block = (
            f"\n\nPREVIOUS PROPOSAL WAS REJECTED. Cross-checker feedback:\n"
            f"{state['feedback']}\n\nIncorporate this feedback into your next proposal."
        )
    return (
        f"Incident: {state['incident_id']}\n"
        f"Detector: {state['detector']} (kind={state['detection_kind']})\n"
        f"Message: {state['detection_message']}\n\n"
        f"Stack trace (if any):\n{state.get('detection_trace') or '(none)'}\n\n"
        f"Log excerpt (last lines):\n{state.get('log_excerpt') or '(none)'}\n"
        f"{feedback_block}\n\n"
        "Use the read_file / grep_repo / view_log_excerpt tools to gather "
        "what you need, then return ONE ProposedAction."
    )


def _prepare_context(state: GraphState) -> GraphState:
    """Initialize per-invocation bookkeeping. Pure function; doesn't
    touch the LLM. Separate node so it shows up in graph visualizations.

    ``attempt`` starts at 0 here; the diagnose node bumps it to 1 on
    first entry. That way every diagnose invocation owns the bump and
    the cross-check conditional edge can be a pure router.
    """
    state["attempt"] = 0
    state["feedback"] = None
    state["proposed"] = None
    state["verdict"] = None
    state["outcome"] = None
    return state


def _build_diagnose_node(cfg: LangGraphConfig, repo_root: Path, incident_folder: Path):
    """Build the diagnose node — an LLM bound to repo tools, returning
    a structured ProposedAction."""
    model = get_chat_model(
        provider=cfg.provider,
        model=cfg.model,
        temperature=cfg.temperature,
        request_timeout_seconds=cfg.request_timeout_seconds,
    )
    tools = _make_tools(repo_root, incident_folder)
    # ``.bind_tools`` lets the model call the tools; we then ask for
    # structured output once it's done exploring. LangChain 1.x's
    # ``with_structured_output`` works across providers.
    tooled = model.bind_tools(tools)
    structured = model.with_structured_output(ProposedAction)

    def node(state: GraphState) -> GraphState:
        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

        # Bump the attempt counter at the start of every diagnose call.
        # LangGraph only persists state that nodes return, so the cross-
        # check conditional edge can't increment this — has to happen
        # here. ``prepare_context`` initializes it to 0; first diagnose
        # call sees attempt=1.
        state["attempt"] = state.get("attempt", 0) + 1
        log().claude(
            f"LangGraph diagnose (attempt {state['attempt']}/{cfg.max_steps}) — "
            f"provider={cfg.provider} model={cfg.model}"
        )
        messages: list[Any] = [
            SystemMessage(content=_DIAGNOSE_SYSTEM),
            HumanMessage(content=_format_detection_prompt(state)),
        ]
        # Bounded tool-call loop. Each iteration: invoke the tooled
        # model; if it returns tool calls, execute them and append the
        # results; otherwise it's the final reasoning message. Then we
        # ask for the structured action one more time.
        for _ in range(8):  # hard cap on tool-call rounds per attempt
            response = tooled.invoke(messages)
            messages.append(response)
            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                break
            tools_by_name = {t.name: t for t in tools}
            for call in tool_calls:
                t = tools_by_name.get(call["name"])
                if t is None:
                    result = f"ERROR: unknown tool {call['name']!r}"
                else:
                    try:
                        result = t.invoke(call.get("args", {}))
                    except Exception as e:  # noqa: BLE001
                        result = f"ERROR: tool {call['name']} raised: {e}"
                messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
        # Final structured-output call. Re-uses the conversation so
        # the model has all the tool context it gathered.
        # ``with_structured_output(ProposedAction)`` returns that exact
        # type at runtime; LangChain's type signature is broader so we
        # cast for the type checker.
        state["proposed"] = cast(ProposedAction, structured.invoke(messages))
        return state

    return node


def _build_cross_check_node(cfg: LangGraphConfig):
    """Build the cross-check node — second model grades the proposal."""
    cc_cfg = cfg.cross_check
    model = get_chat_model(
        provider=cc_cfg.provider,
        model=cc_cfg.model,
        temperature=0.0,  # always deterministic for the verdict
        request_timeout_seconds=cfg.request_timeout_seconds,
    )
    structured = model.with_structured_output(CrossCheckVerdict)

    def node(state: GraphState) -> GraphState:
        from langchain_core.messages import HumanMessage, SystemMessage

        log().claude(f"LangGraph cross_check — provider={cc_cfg.provider} model={cc_cfg.model}")
        proposed = state["proposed"]
        if proposed is None:
            # Graph invariant violation — diagnose always sets proposed.
            # Raise so a regression is loud rather than silently routing
            # an empty proposal to the cross-checker.
            msg = "cross_check invoked but state['proposed'] is None"
            raise RuntimeError(msg)
        prompt = (
            f"Incident: {state['incident_id']}\n"
            f"Detector: {state['detector']}\n"
            f"Detection message: {state['detection_message']}\n\n"
            f"PROPOSED ACTION:\n"
            f"  kind: {proposed.kind}\n"
            f"  set: {proposed.set}\n"
            f"  command: {proposed.command}\n\n"
            f"REASONING:\n{proposed.reasoning}\n\n"
            "Grade this proposal (APPROVE / MODIFY / REJECT)."
        )
        verdict = cast(
            CrossCheckVerdict,
            structured.invoke(
                [
                    SystemMessage(content=_CROSS_CHECK_SYSTEM),
                    HumanMessage(content=prompt),
                ]
            ),
        )
        state["verdict"] = verdict
        # MODIFY adopts the corrected proposal immediately.
        if verdict.verdict == "MODIFY" and verdict.modified_action is not None:
            state["proposed"] = verdict.modified_action
        # REJECT carries the cross-checker's feedback into the next
        # diagnose attempt. Has to be set here (node return) rather than
        # in the conditional edge below, since LangGraph only persists
        # state mutations from nodes.
        if verdict.verdict == "REJECT":
            state["feedback"] = verdict.feedback
        return state

    return node


def _decide_loop_or_finalize(cfg: LangGraphConfig):
    """Conditional edge: REJECT loops back to diagnose (if budget),
    APPROVE / MODIFY proceeds to finalize. Pure router — no state
    mutations, since conditional-edge functions don't persist them."""

    def decide(state: GraphState) -> str:
        verdict = state.get("verdict")
        if verdict is None:
            return "finalize"
        if verdict.verdict == "REJECT":
            # ``attempt`` was bumped by the diagnose node; if we've
            # used our budget, give up.
            if state.get("attempt", 1) >= cfg.max_steps:
                log().recovery(
                    f"LangGraph cross-check REJECTed after {cfg.max_steps} attempts — "
                    f"giving up (returning no action)"
                )
                return "finalize_rejected"
            return "diagnose"
        return "finalize"

    return decide


def _should_cross_check(cfg: LangGraphConfig):
    """Conditional edge after diagnose. Only enters cross_check when
    enabled in config; otherwise jumps straight to finalize."""

    def decide(_state: GraphState) -> str:
        return "cross_check" if cfg.cross_check.enabled else "finalize"

    return decide


def _finalize(state: GraphState) -> GraphState:
    """Convert the proposed action into a HealerOutcome.

    Computes the git diff (if anything was edited via tool calls
    side-effecting) so the incident folder gets the same forensic
    artifact as the legacy Claude healer.
    """
    proposed = state.get("proposed")
    if proposed is None:
        state["outcome"] = None
        return state

    from autosentry.config import RuleAction

    action = RuleAction(
        kind=proposed.kind,
        set=proposed.set,
        command=proposed.command,
        notify=True,
    )
    response_text = (
        f"LangGraph healer outcome:\n"
        f"  kind: {proposed.kind}\n"
        f"  set: {proposed.set}\n"
        f"  command: {proposed.command}\n\n"
        f"REASONING:\n{proposed.reasoning}\n"
    )
    if state.get("verdict") is not None:
        verdict = state["verdict"]
        response_text += f"\nCROSS-CHECK: {verdict.verdict}\nFEEDBACK: {verdict.feedback}\n"
    state["outcome"] = HealerOutcome(
        action=action,
        source="claude",
        claude_response=response_text,
        claude_diff=None,
    )
    return state


def _finalize_rejected(state: GraphState) -> GraphState:
    """Terminal node when the cross-checker has rejected every attempt.
    No action proposed — Monitor falls back to restart_policy."""
    state["outcome"] = None
    log().recovery("LangGraph healer: cross-check rejected all attempts — no action proposed")
    return state


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def build_graph(
    cfg: AutoSentryConfig,
    repo_root: Path,
    incident_folder: Path,
):
    """Assemble the StateGraph for one healer invocation.

    Constructed per-invocation rather than memoized: the tools close
    over ``repo_root`` and ``incident_folder``, both of which change
    between incidents.
    """
    from langgraph.graph import END, StateGraph

    lg_cfg = cfg.healing.langgraph
    # GraphState is a TypedDict; pyrefly's view of LangGraph's
    # ``StateT`` generic constraint doesn't accept the literal class
    # at the call site even though it's the documented usage.
    graph = StateGraph(GraphState)  # pyrefly: ignore[bad-specialization]

    graph.add_node("prepare_context", _prepare_context)
    graph.add_node("diagnose", _build_diagnose_node(lg_cfg, repo_root, incident_folder))
    graph.add_node("cross_check", _build_cross_check_node(lg_cfg))
    graph.add_node("finalize", _finalize)
    graph.add_node("finalize_rejected", _finalize_rejected)

    graph.set_entry_point("prepare_context")
    graph.add_edge("prepare_context", "diagnose")

    graph.add_conditional_edges(
        "diagnose",
        _should_cross_check(lg_cfg),
        {
            "cross_check": "cross_check",
            "finalize": "finalize",
        },
    )
    graph.add_conditional_edges(
        "cross_check",
        _decide_loop_or_finalize(lg_cfg),
        {
            "diagnose": "diagnose",
            "finalize": "finalize",
            "finalize_rejected": "finalize_rejected",
        },
    )
    graph.add_edge("finalize", END)
    graph.add_edge("finalize_rejected", END)

    return graph.compile()


def initial_state_from_detection(
    det: Detection,
    *,
    incident_id: str,
    incident_folder: Path,
    repo_root: Path,
) -> GraphState:
    """Pack a :class:`Detection` + its incident folder into the graph's
    initial state. Read the on-disk log excerpt so the diagnose node
    has it without an extra tool call."""
    log_excerpt_path = incident_folder / "log_excerpt.txt"
    log_excerpt = ""
    if log_excerpt_path.exists():
        try:
            log_excerpt = log_excerpt_path.read_text(encoding="utf-8", errors="replace")
            if len(log_excerpt) > 2000:
                log_excerpt = log_excerpt[-2000:]
        except OSError:
            log_excerpt = ""
    return GraphState(
        incident_id=incident_id,
        incident_folder=str(incident_folder),
        repo_root=str(repo_root),
        detector=det.detector,
        detection_kind=det.kind,
        detection_message=det.message,
        detection_trace=det.trace or "",
        log_excerpt=log_excerpt,
        attempt=1,
        feedback=None,
        proposed=None,
        verdict=None,
        outcome=None,
    )


# Pyrefly's unused-import linter is overzealous about Detection
# (imported only inside TYPE_CHECKING-style at call site).
_ = re
