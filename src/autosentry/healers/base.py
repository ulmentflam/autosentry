"""Healer protocol + outcome type."""

from __future__ import annotations

from dataclasses import dataclass

from autosentry.config import RuleAction


@dataclass
class HealerOutcome:
    """Result of running a healer against a detection."""

    action: RuleAction | None
    # Free-form provenance — which rule, which Claude response, etc. Lands
    # in the incident report so a human can see why the fix was chosen.
    source: str
    rule_name: str | None = None
    claude_response: str | None = None
    claude_diff: str | None = None
