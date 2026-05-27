"""Updater unit tests.

These exercise the pure-logic bits — install-method detection and version
comparison. We don't exercise the actual subprocess calls; those are
proven by the install.sh integration test (and CI installs from PyPI
itself, which is the real end-to-end).
"""

from __future__ import annotations

from autosentry.updater import (
    UpdateCheck,
    _is_stable,
    _version_key,
    detect_install_method,
)


def test_version_key_orders_releases():
    assert _version_key("0.1.0") < _version_key("0.1.1")
    assert _version_key("0.1.0") < _version_key("0.2.0")
    assert _version_key("0.10.0") > _version_key("0.9.9")


def test_version_key_does_not_order_pre_releases_below_stable():
    # _version_key is intentionally naive: it collapses non-digit chunks to
    # their embedded digits, so "0.2.0a1" sorts *above* "0.2.0". That's fine
    # because fetch_latest_version filters pre-releases via _is_stable before
    # comparing — this test pins the behavior so we don't pretend otherwise.
    assert _version_key("0.2.0a1") > _version_key("0.2.0")


def test_is_stable_filters_prereleases():
    assert _is_stable("0.1.0")
    assert _is_stable("1.2.3")
    assert not _is_stable("0.2.0a1")
    assert not _is_stable("0.2.0b2")
    assert not _is_stable("0.2.0rc1")
    assert not _is_stable("0.2.0.dev0")
    assert not _is_stable("0.2.0+local")


def test_update_check_is_outdated_flag():
    older = UpdateCheck(current="0.1.0", latest="0.2.0", is_outdated=True)
    same = UpdateCheck(current="0.1.0", latest="0.1.0", is_outdated=False)
    assert older.is_outdated
    assert not same.is_outdated


def test_detect_install_method_returns_known_value():
    # We can't assert *which* method without staging a fake interpreter path,
    # but the function should always return one of the four known options.
    assert detect_install_method() in {"uv", "pipx", "pip", "unknown"}
