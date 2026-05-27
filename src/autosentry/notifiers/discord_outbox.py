"""File-based Discord outbox notifier."""

from __future__ import annotations

from autosentry.notifiers.file_outbox import FileOutboxNotifier


class DiscordOutboxNotifier(FileOutboxNotifier):
    """Queues notifications into ``discord_outbox.jsonl`` for the dispatcher.

    ``channel`` is a Discord channel id (snowflake). ``thread_key`` is
    autosentry's logical thread label; the dispatcher maps it to a
    Discord thread channel id on first delivery.
    """

    platform = "discord"
