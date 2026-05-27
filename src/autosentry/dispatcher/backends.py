"""Chat delivery backends — Slack and Discord.

Five concrete options behind one :class:`Backend` interface
(``deliver`` outbound, ``fetch_replies`` inbound):

- :class:`StdoutBackend` — prints; useful for MCP mode and dev.
- :class:`WebhookBackend` — Slack incoming webhook (outbound only).
- :class:`SlackApiBackend` — Slack Web API (bidirectional).
- :class:`DiscordWebhookBackend` — Discord webhook (outbound only).
- :class:`DiscordBotBackend` — Discord HTTP API v10 (bidirectional).

Only the *_api / *_bot backends override ``supports_inbound`` to True;
the webhook and stdout backends return empty reply lists.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    # Slack returns a ``ts`` for each message; we keep it so subsequent
    # replies can be threaded under the parent.
    ts: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ReplyMessage:
    """A reply pulled out of a Slack thread."""

    ts: str
    user: str
    text: str
    thread_ts: str | None = None


class Backend(ABC):
    """Backend protocol — outbound delivery + inbound polling."""

    name: str

    @abstractmethod
    def deliver(
        self, *, channel: str | None, text: str, thread_ts: str | None = None
    ) -> DeliveryResult: ...

    @abstractmethod
    def fetch_replies(
        self, *, channel: str | None, thread_ts: str, after_ts: str | None
    ) -> list[ReplyMessage]: ...

    def supports_inbound(self) -> bool:
        return False


class StdoutBackend(Backend):
    """No-op backend; prints what would be sent. Useful for MCP flows.

    In MCP mode an external Claude session reads ``slack_outbox.jsonl``
    directly and posts via its own Slack tools — autosentry just needs
    to record that the entries were queued, which the outbox already
    does. This backend exists so the daemon can be exercised with no
    Slack credentials configured.
    """

    name = "stdout"

    def deliver(
        self, *, channel: str | None, text: str, thread_ts: str | None = None
    ) -> DeliveryResult:
        prefix = f"[{channel or '-'}{':thread' if thread_ts else ''}]"
        print(f"{prefix} {text}")
        return DeliveryResult(ok=True, ts=None)

    def fetch_replies(
        self, *, channel: str | None, thread_ts: str, after_ts: str | None
    ) -> list[ReplyMessage]:
        return []


class WebhookBackend(Backend):
    """POSTs to a Slack incoming webhook URL.

    Slack incoming webhooks are write-only — they can't return ``ts``,
    can't thread replies, and can't read messages. We accept that and
    still let users push event notifications somewhere.
    """

    name = "webhook"

    def __init__(self, url: str) -> None:
        if not url:
            msg = "webhook backend requires a URL (set SLACK_WEBHOOK_URL or --webhook-url)"
            raise ValueError(msg)
        self.url = url

    def deliver(
        self, *, channel: str | None, text: str, thread_ts: str | None = None
    ) -> DeliveryResult:
        payload: dict[str, Any] = {"text": text}
        # Some webhook URLs are tied to a channel; if the user passes one we
        # still set it (Slack will ignore for restricted webhooks).
        if channel:
            payload["channel"] = channel
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
                resp.read()
        except urllib.error.URLError as e:
            return DeliveryResult(ok=False, error=str(e))
        except TimeoutError as e:
            return DeliveryResult(ok=False, error=f"timeout: {e}")
        return DeliveryResult(ok=True, ts=None)

    def fetch_replies(
        self, *, channel: str | None, thread_ts: str, after_ts: str | None
    ) -> list[ReplyMessage]:
        return []


class SlackApiBackend(Backend):
    """Slack Web API backend — supports threads and inbound polling.

    Reads ``SLACK_BOT_TOKEN`` (or accepts one explicitly). Calls
    ``chat.postMessage`` for outbound and ``conversations.replies`` for
    inbound. Keeps things to stdlib HTTP — no slack-sdk dependency.
    """

    name = "slack_api"
    base_url = "https://slack.com/api"

    def __init__(self, token: str, *, bot_user_id: str | None = None) -> None:
        if not token:
            msg = "slack_api backend requires a token (set SLACK_BOT_TOKEN)"
            raise ValueError(msg)
        self.token = token
        # When set, replies authored by this user id are filtered out so the
        # daemon doesn't ingest its own messages as commands.
        self.bot_user_id = bot_user_id

    def supports_inbound(self) -> bool:
        return True

    def deliver(
        self, *, channel: str | None, text: str, thread_ts: str | None = None
    ) -> DeliveryResult:
        if not channel:
            return DeliveryResult(ok=False, error="slack_api requires a channel")
        body: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            body["thread_ts"] = thread_ts
        try:
            payload = self._post("chat.postMessage", body)
        except _SlackHttpError as e:
            return DeliveryResult(ok=False, error=str(e))
        if not payload.get("ok"):
            return DeliveryResult(ok=False, error=payload.get("error") or "unknown")
        return DeliveryResult(ok=True, ts=payload.get("ts"))

    def fetch_replies(
        self, *, channel: str | None, thread_ts: str, after_ts: str | None
    ) -> list[ReplyMessage]:
        if not channel:
            return []
        params: dict[str, Any] = {"channel": channel, "ts": thread_ts, "limit": 200}
        if after_ts:
            params["oldest"] = after_ts
            params["inclusive"] = "false"
        try:
            payload = self._get("conversations.replies", params)
        except _SlackHttpError:
            return []
        if not payload.get("ok"):
            return []
        replies: list[ReplyMessage] = []
        for msg in payload.get("messages", []) or []:
            ts = msg.get("ts")
            user = msg.get("user", "")
            text = msg.get("text", "")
            # Skip the parent message itself.
            if ts == thread_ts:
                continue
            # Filter bot's own messages so commands aren't echoed.
            if self.bot_user_id and user == self.bot_user_id:
                continue
            if not ts or not text:
                continue
            replies.append(ReplyMessage(ts=ts, user=user, text=text, thread_ts=thread_ts))
        return replies

    # ---- HTTP helpers ----

    def _post(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        return _json_request(
            f"{self.base_url}/{method}",
            method="POST",
            body=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            error_tag="slack",
            error_class=_SlackHttpError,
        )

    def _get(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return _json_request(
            f"{self.base_url}/{method}?{urllib.parse.urlencode(params)}",
            method="GET",
            headers={"Authorization": f"Bearer {self.token}"},
            error_tag="slack",
            error_class=_SlackHttpError,
        )


class _SlackHttpError(RuntimeError):
    pass


def _json_request(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    error_tag: str = "http",
    error_class: type[RuntimeError] = RuntimeError,
) -> Any:
    """Issue an HTTP request, parse the JSON response. One implementation
    shared by Slack and Discord backends — they only differ in auth
    headers, base URL, and which error class they want raised."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        msg = f"{error_tag} http error: {e}"
        raise error_class(msg) from e
    except (TimeoutError, json.JSONDecodeError) as e:
        msg = f"{error_tag} response error: {e}"
        raise error_class(msg) from e


# ---------------------------------------------------------------------------
# Discord backends
# ---------------------------------------------------------------------------


class _DiscordHttpError(RuntimeError):
    pass


class DiscordWebhookBackend(Backend):
    """POSTs to a Discord incoming webhook URL.

    Outbound-only — Discord webhooks can't return identifiers that we
    could thread under, and they can't read history. The dispatcher's
    inbound polling is skipped for this backend.

    ``thread_ts`` is interpreted as a Discord *thread channel id*. When
    set, we append ``?thread_id=…`` to the webhook URL so the message
    lands inside that thread.
    """

    name = "discord_webhook"

    def __init__(self, url: str) -> None:
        if not url:
            msg = (
                "discord_webhook backend requires a URL "
                "(set DISCORD_WEBHOOK_URL or --discord-webhook-url)"
            )
            raise ValueError(msg)
        self.url = url

    def deliver(
        self, *, channel: str | None, text: str, thread_ts: str | None = None
    ) -> DeliveryResult:
        # Discord webhook payload — channel is implicit in the URL.
        url = self.url
        if thread_ts:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}thread_id={urllib.parse.quote(thread_ts)}"
        body = json.dumps({"content": text}).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
                resp.read()
        except urllib.error.URLError as e:
            return DeliveryResult(ok=False, error=str(e))
        except TimeoutError as e:
            return DeliveryResult(ok=False, error=f"timeout: {e}")
        return DeliveryResult(ok=True, ts=None)

    def fetch_replies(
        self, *, channel: str | None, thread_ts: str, after_ts: str | None
    ) -> list[ReplyMessage]:
        return []


class DiscordBotBackend(Backend):
    """Discord HTTP API v10 with a bot token. Bidirectional.

    Thread model: Discord threads are distinct channels with their own
    IDs. On first delivery under a ``thread_key`` (no ``thread_ts``
    yet), we post into ``channel`` and then start a thread on that
    message via ``POST /channels/{c}/messages/{m}/threads``; the
    returned thread channel id is returned as the ``ts`` for the
    dispatcher's state to remember. Subsequent messages with that
    ``thread_ts`` post directly into the thread channel.

    Inbound polls ``GET /channels/{thread_channel}/messages?after=<id>``.
    """

    name = "discord_bot"
    base_url = "https://discord.com/api/v10"

    def __init__(
        self,
        token: str,
        *,
        bot_user_id: str | None = None,
        thread_name_prefix: str = "autosentry",
    ) -> None:
        if not token:
            msg = "discord_bot backend requires a token (set DISCORD_BOT_TOKEN)"
            raise ValueError(msg)
        self.token = token
        self.bot_user_id = bot_user_id
        self.thread_name_prefix = thread_name_prefix

    def supports_inbound(self) -> bool:
        return True

    def deliver(
        self, *, channel: str | None, text: str, thread_ts: str | None = None
    ) -> DeliveryResult:
        if not channel:
            return DeliveryResult(ok=False, error="discord_bot requires a channel id")
        # Already inside a thread channel? Post directly there.
        if thread_ts:
            return self._post_message(thread_ts, text)
        # First message under this thread_key: post into the channel,
        # then start a thread on the returned message and return the
        # thread channel id as our ``ts``.
        first = self._post_message(channel, text)
        if not first.ok or not first.ts:
            return first
        try:
            payload = self._post(
                f"channels/{channel}/messages/{first.ts}/threads",
                {
                    "name": f"{self.thread_name_prefix}-{first.ts}",
                    # 1440 minutes = 24h auto-archive.
                    "auto_archive_duration": 1440,
                },
            )
        except _DiscordHttpError as e:
            # We still delivered the message; just couldn't start the
            # thread. Caller can decide whether to retry.
            return DeliveryResult(
                ok=True,
                ts=first.ts,
                error=f"posted but failed to open thread: {e}",
            )
        thread_id = payload.get("id")
        if not thread_id:
            return DeliveryResult(ok=True, ts=first.ts)
        # Use the thread channel id as the "ts" so the dispatcher's state
        # maps thread_key → thread channel id, and subsequent posts route
        # via the thread_ts=thread_id branch above.
        return DeliveryResult(ok=True, ts=str(thread_id))

    def fetch_replies(
        self, *, channel: str | None, thread_ts: str, after_ts: str | None
    ) -> list[ReplyMessage]:
        # In Discord land, thread_ts is the thread channel id.
        thread_channel = thread_ts
        params: dict[str, Any] = {"limit": 100}
        if after_ts:
            params["after"] = after_ts
        try:
            payload = self._get(f"channels/{thread_channel}/messages", params)
        except _DiscordHttpError:
            return []
        # Discord returns newest-first; flip for monotonic processing.
        messages = list(reversed(payload if isinstance(payload, list) else []))
        replies: list[ReplyMessage] = []
        for msg in messages:
            mid = str(msg.get("id") or "")
            content = msg.get("content") or ""
            author = msg.get("author") or {}
            author_id = str(author.get("id") or "")
            if not mid or not content:
                continue
            # Filter the bot's own messages.
            if self.bot_user_id and author_id == self.bot_user_id:
                continue
            if author.get("bot"):
                # Skip other bots' messages too — humans only.
                continue
            replies.append(
                ReplyMessage(
                    ts=mid,
                    user=author_id,
                    text=content,
                    thread_ts=thread_channel,
                )
            )
        return replies

    # ---- HTTP helpers ----

    def _post_message(self, channel: str, text: str) -> DeliveryResult:
        try:
            payload = self._post(f"channels/{channel}/messages", {"content": text})
        except _DiscordHttpError as e:
            return DeliveryResult(ok=False, error=str(e))
        ts = payload.get("id")
        if not ts:
            return DeliveryResult(ok=False, error="discord: missing message id")
        return DeliveryResult(ok=True, ts=str(ts))

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        return _json_request(
            f"{self.base_url}/{path}",
            method="POST",
            body=body,
            headers=self._headers(post=True),
            error_tag="discord",
            error_class=_DiscordHttpError,
        )

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        return _json_request(
            f"{self.base_url}/{path}?{urllib.parse.urlencode(params)}",
            method="GET",
            headers=self._headers(),
            error_tag="discord",
            error_class=_DiscordHttpError,
        )

    def _headers(self, *, post: bool = False) -> dict[str, str]:
        h = {
            "Authorization": f"Bot {self.token}",
            "User-Agent": "autosentry (https://github.com/ulmentflam/autosentry, 0.5.0)",
        }
        if post:
            h["Content-Type"] = "application/json; charset=utf-8"
        return h
