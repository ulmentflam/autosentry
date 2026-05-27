"""Notifier protocol."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


@dataclass
class Notification:
    kind: Literal["start", "anomaly", "recovery", "milestone", "exit", "heartbeat"]
    title: str
    body: str
    incident_id: str | None = None


class Notifier(ABC):
    @abstractmethod
    def notify(self, n: Notification) -> None: ...
