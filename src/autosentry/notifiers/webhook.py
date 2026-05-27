"""Webhook notifier — POSTs JSON to a configured URL.

Kept minimal and dependency-free (uses urllib). For richer delivery
(retries, headers, signing) drop in a custom Notifier subclass.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import asdict

from autosentry.logger import log
from autosentry.notifiers.base import Notification, Notifier


class WebhookNotifier(Notifier):
    def __init__(self, *, url: str) -> None:
        if not url:
            msg = "webhook notifier requires 'url'"
            raise ValueError(msg)
        self.url = url

    def notify(self, n: Notification) -> None:
        payload = json.dumps(asdict(n)).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 — http(s) only, user-configured
            self.url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            log().error(f"webhook delivery failed: {e}")
