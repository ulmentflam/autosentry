"""Discord dispatcher backend tests.

These don't touch the real Discord API. They wrap ``urlopen`` to assert
the requests autosentry would have made, and exercise the dispatcher
end-to-end against a fake backend so the threading + delivery contract
is verified.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from autosentry.dispatcher import (
    DiscordBotBackend,
    DiscordWebhookBackend,
    DispatcherDaemon,
)
from autosentry.notifiers import DiscordOutboxNotifier
from autosentry.notifiers.base import Notification

# ----- DiscordWebhookBackend -----------------------------------------------


def _fake_response(body: dict | list | str | bytes = b""):
    """Build a minimal urllib-style response context manager."""
    if isinstance(body, (dict, list)):
        payload = json.dumps(body).encode("utf-8")
    elif isinstance(body, str):
        payload = body.encode("utf-8")
    else:
        payload = body
    mock = MagicMock()
    mock.__enter__.return_value.read.return_value = payload
    mock.__exit__.return_value = False
    return mock


def test_discord_webhook_requires_url():
    with pytest.raises(ValueError, match="DISCORD_WEBHOOK_URL"):
        DiscordWebhookBackend("")


def test_discord_webhook_posts_content_payload():
    backend = DiscordWebhookBackend("https://discord.com/api/webhooks/1/abc")
    captured: list = []

    def fake_urlopen(req, timeout=15):
        captured.append((req.full_url, req.data))
        return _fake_response("")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = backend.deliver(channel="ignored", text="hello", thread_ts=None)

    assert result.ok
    assert result.ts is None
    url, data = captured[0]
    assert url == "https://discord.com/api/webhooks/1/abc"
    assert json.loads(data) == {"content": "hello"}


def test_discord_webhook_routes_into_thread_via_query_param():
    backend = DiscordWebhookBackend("https://discord.com/api/webhooks/1/abc")
    captured: list = []

    def fake_urlopen(req, timeout=15):
        captured.append(req.full_url)
        return _fake_response("")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        backend.deliver(channel=None, text="reply", thread_ts="9999")

    assert "thread_id=9999" in captured[0]


def test_discord_webhook_does_not_support_inbound():
    backend = DiscordWebhookBackend("https://x")
    assert backend.supports_inbound() is False
    assert backend.fetch_replies(channel=None, thread_ts="t", after_ts=None) == []


# ----- DiscordBotBackend ---------------------------------------------------


def test_discord_bot_requires_token():
    with pytest.raises(ValueError, match="DISCORD_BOT_TOKEN"):
        DiscordBotBackend("")


def test_discord_bot_first_message_opens_thread_and_returns_thread_id():
    backend = DiscordBotBackend("BOT_TOKEN", bot_user_id="UBOT")
    calls: list[tuple[str, dict]] = []

    def fake_urlopen(req, timeout=15):
        path = req.full_url
        body = json.loads(req.data) if req.data else {}
        calls.append((path, body))
        # First POST → message endpoint. Second → threads endpoint.
        if path.endswith("/messages"):
            return _fake_response({"id": "MSG1"})
        if "/threads" in path:
            return _fake_response({"id": "THREAD1"})
        return _fake_response({})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = backend.deliver(channel="CHAN1", text="first message", thread_ts=None)

    assert result.ok
    assert result.ts == "THREAD1"  # returned thread channel id, not the msg id
    assert calls[0][0].endswith("channels/CHAN1/messages")
    assert calls[1][0].endswith("channels/CHAN1/messages/MSG1/threads")


def test_discord_bot_subsequent_messages_post_into_thread_directly():
    backend = DiscordBotBackend("BOT_TOKEN")
    calls: list[str] = []

    def fake_urlopen(req, timeout=15):
        calls.append(req.full_url)
        return _fake_response({"id": "MSG2"})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = backend.deliver(channel="CHAN1", text="follow-up", thread_ts="THREAD1")

    assert result.ok
    # Only one HTTP call: directly to the thread channel's messages endpoint.
    # No separate threads call.
    assert len(calls) == 1
    assert calls[0].endswith("channels/THREAD1/messages")


def test_discord_bot_fetch_replies_filters_bots_and_self():
    backend = DiscordBotBackend("BOT_TOKEN", bot_user_id="UBOT")

    def fake_urlopen(req, timeout=15):
        # Discord returns newest-first; ensure backend reverses to oldest-first.
        return _fake_response(
            [
                {
                    "id": "M3",
                    "content": "abort",
                    "author": {"id": "UHUMAN", "bot": False},
                },
                {
                    "id": "M2",
                    "content": "noise from another bot",
                    "author": {"id": "UOTHER", "bot": True},
                },
                {
                    "id": "M1",
                    "content": "i posted this",
                    "author": {"id": "UBOT", "bot": False},
                },
            ]
        )

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        replies = backend.fetch_replies(channel=None, thread_ts="THREAD1", after_ts="M0")

    # Only the human's M3 survives the filter.
    assert [(r.ts, r.user, r.text) for r in replies] == [("M3", "UHUMAN", "abort")]


def test_discord_bot_deliver_requires_channel():
    backend = DiscordBotBackend("BOT_TOKEN")
    out = backend.deliver(channel=None, text="x", thread_ts=None)
    assert out.ok is False
    assert "channel" in (out.error or "")


# ----- end-to-end via dispatcher -------------------------------------------


def test_dispatcher_routes_discord_outbox_through_bot_backend(tmp_path: Path):
    """One-tick of the dispatcher: drains the outbox + opens a thread."""
    outbox = tmp_path / "discord_outbox.jsonl"
    outbox.write_text(
        json.dumps(
            {
                "id": "msg-1",
                "channel": "CHAN1",
                "thread_key": "pipeline",
                "kind": "anomaly",
                "title": "Stall",
                "body": "step 8450 has not advanced",
                "sent": False,
            }
        )
        + "\n"
    )

    calls: list[str] = []

    def fake_urlopen(req, timeout=15):
        calls.append(req.full_url)
        if req.full_url.endswith("/messages"):
            return _fake_response({"id": "M1"})
        if "/threads" in req.full_url:
            return _fake_response({"id": "THREAD-A"})
        return _fake_response({})

    backend = DiscordBotBackend("BOT", bot_user_id="UBOT")
    daemon = DispatcherDaemon(
        backend=backend,
        outbox_path=outbox,
        inbox_path=tmp_path / "discord_inbox.jsonl",
        state_path=tmp_path / "dispatcher_state.json",
        default_channel="CHAN1",
        idle_inbound_seconds=0,
    )
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        daemon.tick_once()

    # Outbox row marked sent and thread_ts pinned.
    rewritten = [json.loads(line) for line in outbox.read_text().splitlines() if line.strip()]
    assert rewritten[0]["sent"] is True
    assert rewritten[0]["thread_ts"] == "THREAD-A"

    # State remembers the thread map.
    state = json.loads((tmp_path / "dispatcher_state.json").read_text())
    assert state["threads"]["pipeline"] == "THREAD-A"


# ----- DiscordOutboxNotifier -----------------------------------------------


def test_discord_outbox_notifier_writes_jsonl(tmp_path: Path):
    outbox = tmp_path / "discord_outbox.jsonl"
    notifier = DiscordOutboxNotifier(
        outbox_path=str(outbox),
        channel="123456789012345678",
        thread_key="pipeline",
    )
    notifier.notify(
        Notification(kind="anomaly", title="Stall", body="step 8450", incident_id="inc-1")
    )
    line = outbox.read_text().strip()
    data = json.loads(line)
    assert data["channel"] == "123456789012345678"
    assert data["thread_key"] == "pipeline"
    assert data["kind"] == "anomaly"
    assert data["sent"] is False


def test_slack_and_discord_outbox_use_same_wire_format(tmp_path: Path):
    """The two outboxes must share format so the dispatcher's load/save
    logic doesn't fork."""
    from autosentry.notifiers import SlackOutboxNotifier

    slack_path = tmp_path / "slack.jsonl"
    discord_path = tmp_path / "discord.jsonl"
    slack = SlackOutboxNotifier(outbox_path=str(slack_path), channel="C1", thread_key="pipeline")
    discord = DiscordOutboxNotifier(
        outbox_path=str(discord_path), channel="C1", thread_key="pipeline"
    )
    n = Notification(kind="info", title="hi", body="b")
    slack.notify(n)
    discord.notify(n)
    slack_keys = set(json.loads(slack_path.read_text()).keys())
    discord_keys = set(json.loads(discord_path.read_text()).keys())
    assert slack_keys == discord_keys
