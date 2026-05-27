"""YAML-rule healer.

Walks the configured rule list in order; the first rule whose ``match``
clause is satisfied by the detection wins. A match clause requires the
detection's ``detector`` name to equal ``match.detector``, and (if set)
``match.message_regex`` to find a hit in the detection's message.
"""

from __future__ import annotations

import re

from autosentry.config import Rule
from autosentry.detectors.base import Detection
from autosentry.healers.base import HealerOutcome


class RuleHealer:
    def __init__(self, rules: list[Rule]) -> None:
        self.rules = rules
        # Pre-compile message regexes.
        self._compiled: list[tuple[Rule, re.Pattern[str] | None]] = []
        for r in rules:
            pattern = re.compile(r.match.message_regex) if r.match.message_regex else None
            self._compiled.append((r, pattern))

    def attempt(self, det: Detection) -> HealerOutcome | None:
        for rule, msg_re in self._compiled:
            if rule.match.detector != det.detector:
                continue
            if msg_re is not None and not msg_re.search(det.message):
                continue
            return HealerOutcome(
                action=rule.action,
                source="rule",
                rule_name=rule.name or rule.match.detector,
            )
        return None
