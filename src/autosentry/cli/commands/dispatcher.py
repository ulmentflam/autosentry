"""``autosentry dispatcher`` subcommands (Slack + Discord)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import typer

from autosentry.cli import app
from autosentry.cli.style import ERR, WARN, console
from autosentry.config import DEFAULT_CONFIG_PATH, load_config

dispatcher_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Bidirectional chat dispatcher daemon "
        "(Slack + Discord; outbox drain + inbound reply polling)."
    ),
)
app.add_typer(dispatcher_app, name="dispatcher")


@dispatcher_app.command("run")
def dispatcher_run(
    config: Path = typer.Option(  # noqa: B008
        DEFAULT_CONFIG_PATH, "--config", "-c"
    ),
    backend: str = typer.Option(
        "",
        "--backend",
        help=(
            "Force backend: stdout | webhook | slack_api | discord_webhook | "
            "discord_bot. Default: auto-detect from credentials."
        ),
    ),
    webhook_url: str = typer.Option(
        "",
        "--webhook-url",
        envvar="SLACK_WEBHOOK_URL",
        help="Slack incoming webhook URL.",
    ),
    slack_token: str = typer.Option(
        "",
        "--slack-token",
        envvar="SLACK_BOT_TOKEN",
        help="Slack bot token (slack_api backend, e.g. xoxb-…).",
    ),
    discord_webhook_url: str = typer.Option(
        "",
        "--discord-webhook-url",
        envvar="DISCORD_WEBHOOK_URL",
        help="Discord incoming webhook URL.",
    ),
    discord_token: str = typer.Option(
        "",
        "--discord-token",
        envvar="DISCORD_BOT_TOKEN",
        help="Discord bot token (discord_bot backend).",
    ),
    bot_user_id: str = typer.Option(
        "",
        "--bot-user-id",
        envvar="SLACK_BOT_USER_ID",
        help="Filter the bot's own messages out of inbound polling (Slack).",
    ),
    discord_bot_user_id: str = typer.Option(
        "",
        "--discord-bot-user-id",
        envvar="DISCORD_BOT_USER_ID",
        help="Filter the bot's own messages out of inbound polling (Discord).",
    ),
    channel: str = typer.Option(
        "",
        "--channel",
        help=(
            "Default channel identifier (Slack channel id OR Discord channel id). "
            "Overridden per-message by the outbox entry."
        ),
    ),
    poll_seconds: float = typer.Option(
        30.0,
        "--poll-seconds",
        help=(
            "Loop cadence in seconds. The loop is mtime-gated so this is "
            "really just the file-stat interval."
        ),
    ),
    once: bool = typer.Option(
        False, "--once", help="Run a single tick and exit (useful in scripts/CI)."
    ),
    inbox_path: Path = typer.Option(  # noqa: B008
        Path(".autosentry/slack_inbox.jsonl"),
        "--inbox-path",
        help="Where to append observed thread replies.",
    ),
    state_path: Path = typer.Option(  # noqa: B008
        Path(".autosentry/dispatcher_state.json"),
        "--state-path",
        help="Where the dispatcher persists its thread map.",
    ),
    inbound_marker: Path = typer.Option(  # noqa: B008
        Path(".autosentry/inbox_poll_request"),
        "--inbound-marker",
        help=(
            "Marker file the monitor touches on detection fires. The "
            "dispatcher polls Slack thread replies only when its mtime "
            "advances (or after --idle-inbound-seconds)."
        ),
    ),
    idle_inbound_seconds: float = typer.Option(
        300.0,
        "--idle-inbound-seconds",
        help=(
            "Long-period inbound poll interval — catches replies sent "
            "during stretches without detection activity. Set to 0 to "
            "rely entirely on the marker file."
        ),
    ),
) -> None:
    """Bidirectional chat dispatcher daemon (Slack + Discord).

    Outbound: drains `slack_outbox.jsonl` / `discord_outbox.jsonl` and
    delivers each pending entry through the chosen backend.

    Inbound (`slack_api` / `discord_bot` only): polls the parent thread
    for human replies and writes them to the matching inbox file so
    the monitor can apply recognized commands (`abort`, `pause`,
    `resume`, `set max_restarts N`, `approve`, `comment: …`).

    The loop is lazy: mtime-gated outbox drain + marker-driven inbound
    polling. Backend defaults to whatever credentials are configured
    in env (SLACK_BOT_TOKEN, DISCORD_BOT_TOKEN, SLACK_WEBHOOK_URL,
    DISCORD_WEBHOOK_URL) — set --backend to override.

    Long-running by design; pair with --once to drain a single tick
    and exit (useful in CI / cron).
    """
    from autosentry.dispatcher import (
        DiscordBotBackend,
        DiscordWebhookBackend,
        DispatcherDaemon,
        SlackApiBackend,
        StdoutBackend,
        WebhookBackend,
    )

    cfg = load_config(config)
    chosen = backend or _detect_backend(
        slack_webhook=webhook_url,
        slack_token=slack_token,
        discord_webhook=discord_webhook_url,
        discord_token=discord_token,
    )
    outbox_kind = (
        "discord_outbox" if chosen in ("discord_bot", "discord_webhook") else "slack_outbox"
    )
    outboxes = [n for n in cfg.notifiers if n.kind == outbox_kind]
    if not outboxes:
        console.print(
            f"[{WARN}]no {outbox_kind} notifier configured (needed for --backend {chosen})[/{WARN}]"
        )
        raise typer.Exit(code=1)
    outbox_path = cfg.resolve(outboxes[0].outbox_path)
    default_channel = channel or outboxes[0].channel

    if chosen == "slack_api":
        chosen_backend = SlackApiBackend(slack_token, bot_user_id=bot_user_id or None)
    elif chosen == "webhook":
        chosen_backend = WebhookBackend(webhook_url)
    elif chosen == "discord_bot":
        chosen_backend = DiscordBotBackend(discord_token, bot_user_id=discord_bot_user_id or None)
    elif chosen == "discord_webhook":
        chosen_backend = DiscordWebhookBackend(discord_webhook_url)
    else:
        chosen_backend = StdoutBackend()

    # Per-platform default inbox/state paths so a Slack daemon and a
    # Discord daemon can run side by side without colliding.
    if outbox_kind == "discord_outbox":
        if str(inbox_path) == ".autosentry/slack_inbox.jsonl":
            inbox_path = Path(".autosentry/discord_inbox.jsonl")
        if str(state_path) == ".autosentry/dispatcher_state.json":
            state_path = Path(".autosentry/discord_dispatcher_state.json")
        if str(inbound_marker) == ".autosentry/inbox_poll_request":
            inbound_marker = Path(".autosentry/discord_inbox_poll_request")

    daemon = DispatcherDaemon(
        backend=chosen_backend,
        outbox_path=outbox_path,
        inbox_path=cfg.resolve(str(inbox_path)),
        state_path=cfg.resolve(str(state_path)),
        poll_seconds=poll_seconds,
        default_channel=default_channel,
        inbound_marker_path=cfg.resolve(str(inbound_marker)),
        idle_inbound_seconds=idle_inbound_seconds,
    )
    if once:
        daemon.tick_once()
        return
    try:
        daemon.run()
    except KeyboardInterrupt:
        daemon.stop()


@dispatcher_app.command("pending")
def dispatcher_pending(
    config: Path = typer.Option(  # noqa: B008
        DEFAULT_CONFIG_PATH, "--config", "-c"
    ),
) -> None:
    """List currently pending (unsent) outbox entries — no delivery."""
    cfg = load_config(config)
    outboxes = [n for n in cfg.notifiers if n.kind in ("slack_outbox", "discord_outbox")]
    if not outboxes:
        console.print(f"[{WARN}]no slack_outbox or discord_outbox notifier configured[/{WARN}]")
        raise typer.Exit(code=1)
    outbox_path = cfg.resolve(outboxes[0].outbox_path)
    entries = list(_iter_outbox_entries(outbox_path))
    if not entries:
        console.print("[dim]outbox empty[/dim]")
        return
    pending = [e for e in entries if not e.get("sent")]
    console.print(f"[bold]{len(pending)} pending[/bold] of {len(entries)} total")
    for e in pending:
        body = (e.get("body") or "")[:100]
        console.print(f"  → [{e.get('kind')}] {e.get('title')}: {body}")
    _ = ERR  # placate linter when no error branches fire


def _iter_outbox_entries(path: Path) -> Iterator[dict]:
    """Yield each JSON-decoded entry from an outbox file. Tolerates malformed
    lines silently — same behavior as the daemon's loader."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _detect_backend(
    *,
    slack_webhook: str = "",
    slack_token: str = "",
    discord_webhook: str = "",
    discord_token: str = "",
) -> str:
    """Auto-pick a dispatcher backend, preferring the most capable creds.

    Order: slack_api → discord_bot → webhook (Slack) → discord_webhook
    → stdout. ``--backend`` overrides this.
    """
    if slack_token:
        return "slack_api"
    if discord_token:
        return "discord_bot"
    if slack_webhook:
        return "webhook"
    if discord_webhook:
        return "discord_webhook"
    return "stdout"
