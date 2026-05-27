"""Bidirectional dispatcher daemon.

Outbound: drains ``<backend>_outbox.jsonl`` files (Slack and/or Discord)
and delivers each pending entry through a configured backend (webhook,
Slack Web API, Discord webhook, Discord bot API, or stdout).

Inbound (only for backends that support it — slack_api and discord_bot):
polls the parent thread for human replies and appends them to
``<backend>_inbox.jsonl`` so the monitor can act on commands (``abort``,
``pause``, ``resume``, ``set max_restarts N``, ``approve``,
``comment: …``).
"""

from autosentry.dispatcher.backends import (
    Backend,
    DeliveryResult,
    DiscordBotBackend,
    DiscordWebhookBackend,
    SlackApiBackend,
    StdoutBackend,
    WebhookBackend,
)
from autosentry.dispatcher.daemon import (
    DispatcherDaemon,
    DispatcherState,
    InboxEntry,
    OutboxEntry,
    parse_command,
)

__all__ = [
    "Backend",
    "DeliveryResult",
    "DiscordBotBackend",
    "DiscordWebhookBackend",
    "DispatcherDaemon",
    "DispatcherState",
    "InboxEntry",
    "OutboxEntry",
    "SlackApiBackend",
    "StdoutBackend",
    "WebhookBackend",
    "parse_command",
]
