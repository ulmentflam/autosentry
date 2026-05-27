"""Notifier protocol + built-in notifiers."""

from autosentry.notifiers.base import Notification, Notifier
from autosentry.notifiers.discord_outbox import DiscordOutboxNotifier
from autosentry.notifiers.file_outbox import FileOutboxNotifier
from autosentry.notifiers.log import LogNotifier
from autosentry.notifiers.slack_outbox import SlackOutboxNotifier


def build_notifiers(specs):
    """Instantiate notifiers from NotifierSpec list."""
    out: list[Notifier] = []
    for spec in specs:
        if spec.kind == "log":
            out.append(LogNotifier())
        elif spec.kind == "slack_outbox":
            out.append(
                SlackOutboxNotifier(
                    outbox_path=spec.outbox_path,
                    channel=spec.channel,
                    thread_key=spec.thread_key,
                )
            )
        elif spec.kind == "discord_outbox":
            out.append(
                DiscordOutboxNotifier(
                    outbox_path=spec.outbox_path,
                    channel=spec.channel,
                    thread_key=spec.thread_key,
                )
            )
        elif spec.kind == "webhook":
            from autosentry.notifiers.webhook import WebhookNotifier

            out.append(WebhookNotifier(url=spec.url or ""))
    return out


__all__ = [
    "DiscordOutboxNotifier",
    "FileOutboxNotifier",
    "LogNotifier",
    "Notification",
    "Notifier",
    "SlackOutboxNotifier",
    "build_notifiers",
]
