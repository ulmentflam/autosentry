"""Shared rich console, color tokens, banners, and prompt helpers.

One Console instance for the whole CLI so we have a single place to
configure colorization, width detection, and TTY behavior. When stdout
isn't a TTY (piped to a file, captured by an AI agent), we strip
colors automatically — that keeps logs clean and lets agents parse
output without ANSI noise.

The color tokens (``OK``, ``WARN``, ``ERR``, ``INFO``, ``DIM``,
``ACCENT``) are intentionally a small fixed palette so the whole CLI
feels consistent. Use them inline in rich markup, e.g.::

    console.print(f"[{OK}]done[/{OK}] · {n} files written")
"""

from __future__ import annotations

import os
import sys

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text
from rich.theme import Theme

# ---- color tokens ----------------------------------------------------------

OK = "green"
WARN = "yellow"
ERR = "red"
INFO = "cyan"
DIM = "dim"
ACCENT = "magenta"

_THEME = Theme(
    {
        "ok": OK,
        "warn": WARN,
        "err": ERR,
        "info": INFO,
        "dim": DIM,
        "accent": ACCENT,
        "kv.key": "dim",
        "kv.value": "white",
    }
)


# ---- console singleton -----------------------------------------------------


def _is_piped() -> bool:
    """``True`` when stdout isn't a real terminal (CI, pipe, AI agent capture)."""
    return not sys.stdout.isatty()


def _force_color() -> bool:
    """Respect ``FORCE_COLOR`` / ``CLICOLOR_FORCE`` for piped output that wants
    color anyway (e.g. piping to ``less -R``)."""
    return bool(os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE"))


def _no_color() -> bool:
    """Respect the widely-adopted ``NO_COLOR=1`` opt-out."""
    return bool(os.environ.get("NO_COLOR"))


def make_console() -> Console:
    if _no_color():
        return Console(theme=_THEME, no_color=True)
    if _is_piped() and not _force_color():
        return Console(theme=_THEME, no_color=True, force_terminal=False)
    return Console(theme=_THEME, force_terminal=True)


console: Console = make_console()


# ---- banner / hero ---------------------------------------------------------


_BANNER_ASCII = r"""
                _                       _
   __ _ _   _ | |_ ___  ___  ___ _ __ | |_ _ __ _   _
  / _` | | | || __/ _ \/ __|/ _ \ '_ \| __| '__| | | |
 | (_| | |_| || || (_) \__ \  __/ | | | |_| |  | |_| |
  \__,_|\__,_| \__\___/|___/\___|_| |_|\__|_|   \__, |
                                                |___/
"""


def banner(
    version: str, *, tagline: str = "self-healing supervisor for long-running processes"
) -> Text:
    """A small rich-rendered hero suitable for ``--version`` / ``init``."""
    text = Text()
    text.append(_BANNER_ASCII, style=ACCENT)
    text.append(f"  v{version}", style="bold white")
    text.append(f"   {tagline}\n", style=DIM)
    return text


# ---- key-value tables ------------------------------------------------------


def kv_panel(title: str, rows: list[tuple[str, str]], *, border: str = OK) -> Panel:
    """A compact ``Panel`` of ``key: value`` rows for status output."""
    from rich.table import Table

    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style=DIM)
    t.add_column()
    for k, v in rows:
        t.add_row(k, v)
    return Panel(t, title=title, border_style=border)


# ---- prompts (rich.Prompt thin wrappers, TTY-aware) ------------------------


def is_interactive() -> bool:
    """Both stdin and stdout must be TTYs for an interactive prompt."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(question: str, *, default: str | None = None) -> str:
    """``rich.Prompt.ask`` with our themed console. Returns ``default`` if
    the session is non-interactive (and ``default`` is set)."""
    if not is_interactive() and default is not None:
        return default
    return Prompt.ask(question, default=default or "", console=console)


def confirm(question: str, *, default: bool = False) -> bool:
    """``rich.Confirm.ask`` with our themed console. Returns ``default`` if
    the session is non-interactive."""
    if not is_interactive():
        return default
    return Confirm.ask(question, default=default, console=console)


# ---- one-shot helpers commands use a lot -----------------------------------


def ok(msg: str) -> None:
    console.print(f"[{OK}]✓[/{OK}] {msg}")


def warn(msg: str) -> None:
    console.print(f"[{WARN}]![/{WARN}] {msg}")


def err(msg: str) -> None:
    console.print(f"[{ERR}]✗[/{ERR}] {msg}")


def info(msg: str) -> None:
    console.print(f"[{INFO}]·[/{INFO}] {msg}")


def hint(msg: str) -> None:
    console.print(f"[{DIM}]{msg}[/{DIM}]")
