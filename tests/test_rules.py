"""Rule healer tests."""

from __future__ import annotations

from autosentry.config import Rule, RuleAction, RuleMatch
from autosentry.detectors.base import Detection
from autosentry.healers.rules import RuleHealer


def test_first_rule_match_wins():
    rules = [
        Rule(
            name="oom_rule",
            match=RuleMatch(detector="oom"),
            action=RuleAction(kind="restart_with_env", set={"BATCH_SIZE": "half"}),
        ),
        Rule(
            name="catchall",
            match=RuleMatch(detector="oom"),
            action=RuleAction(kind="abort"),
        ),
    ]
    healer = RuleHealer(rules)
    det = Detection(detector="oom", kind="error", message="OOM at step 5")
    out = healer.attempt(det)
    assert out is not None
    assert out.rule_name == "oom_rule"
    assert out.action.kind == "restart_with_env"


def test_no_match_returns_none():
    rules = [
        Rule(
            name="nccl_rule",
            match=RuleMatch(detector="nccl"),
            action=RuleAction(kind="restart"),
        )
    ]
    healer = RuleHealer(rules)
    det = Detection(detector="oom", kind="error", message="oops")
    assert healer.attempt(det) is None


def test_message_regex_filter():
    rules = [
        Rule(
            name="big_oom",
            match=RuleMatch(detector="oom", message_regex=r"more than \d+ GiB"),
            action=RuleAction(kind="restart_with_env", set={"BATCH_SIZE": "half"}),
        ),
    ]
    healer = RuleHealer(rules)
    assert healer.attempt(Detection(detector="oom", kind="error", message="hi")) is None
    assert (
        healer.attempt(
            Detection(detector="oom", kind="error", message="tried to alloc more than 4 GiB")
        )
        is not None
    )
