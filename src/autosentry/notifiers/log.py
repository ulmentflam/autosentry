"""Notifier that just writes to the structured log."""

from __future__ import annotations

from autosentry.logger import log
from autosentry.notifiers.base import Notification, Notifier


class LogNotifier(Notifier):
    def notify(self, n: Notification) -> None:
        msg = f"{n.title} — {n.body}"
        if n.incident_id:
            msg += f" (incident: {n.incident_id})"
        if n.kind == "anomaly":
            log().anomaly(msg)
        elif n.kind == "recovery":
            log().recovery(msg)
        else:
            log().info(msg)
