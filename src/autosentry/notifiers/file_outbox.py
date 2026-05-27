"""Shared file-outbox notifier base.

Both Slack and Discord dispatchers drain a JSON-lines file written by
the monitor. The schema is identical; only the destination file and
the log prefix differ. Subclasses customize via ``platform`` (used in
the log prefix) and ``default_outbox_path``.

The point of the indirection — same as in the original rad_monitor.py
— is that the monitor can run on a restricted machine (SLURM head node,
container without outbound network) and the dispatcher can run
anywhere.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from autosentry.logger import log
from autosentry.notifiers.base import Notification, Notifier


class FileOutboxNotifier(Notifier):
    """JSON-lines outbox shared between Slack and Discord.

    Subclasses set ``platform`` (used only in the log line) and inherit
    everything else. The ``channel`` and ``thread_key`` fields are
    platform-specific identifiers; the dispatcher backend on the other
    end interprets them.
    """

    platform: str = "outbox"

    def __init__(
        self,
        *,
        outbox_path: str,
        channel: str | None,
        thread_key: str | None,
    ) -> None:
        self.outbox_path = Path(outbox_path)
        self.channel = channel
        self.thread_key = thread_key

    def notify(self, n: Notification) -> None:
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "id": f"{time.time():.6f}",
            "channel": self.channel,
            "thread_key": self.thread_key,
            "kind": n.kind,
            "title": n.title,
            "body": n.body,
            "incident_id": n.incident_id,
            "sent": False,
        }
        with self.outbox_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        # The structured logger has a dedicated SLACK level — we reuse it
        # for any chat platform; the prefix in the message disambiguates.
        log().slack(f"[{self.platform}] queued [{n.kind}] {n.title}")
