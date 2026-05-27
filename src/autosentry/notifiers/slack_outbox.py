"""File-based Slack outbox notifier."""

from __future__ import annotations

from autosentry.notifiers.file_outbox import FileOutboxNotifier


class SlackOutboxNotifier(FileOutboxNotifier):
    """Queues notifications into ``slack_outbox.jsonl`` for the dispatcher.

    Identical wire format as the Discord outbox; the dispatcher backend
    on the other end is what routes a queued entry to Slack vs Discord.
    """

    platform = "slack"
