#!/usr/bin/env python3
"""Generate Homebrew `resource` stanzas for autosentry's dependency closure.

Why this exists: `brew update-python-resources` is the canonical tool, but it's
a Homebrew *developer* command that writes into the vendored bundle dir — which
is read-only under nix-managed Homebrew. So we resolve the closure with `uv`
(authoritative, respects markers) and pull each package's **sdist** URL + sha256
straight from the PyPI JSON API, since Homebrew builds Python deps from source.

Usage:
    python3 gen_resources.py <package==version> [--python 3.13]

Emits resource blocks to stdout, sorted alphabetically (matches brew style).
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

PYPI = "https://pypi.org/pypi/{name}/{version}/json"
PYPI_TIMEOUT = 30  # seconds — fail fast instead of hanging on a stalled connection


def resolve(spec: str, python: str) -> list[tuple[str, str]]:
    """Return [(name, version), ...] for the full closure, excluding the root."""
    root_name = spec.split("==")[0].replace("_", "-").lower()
    out = subprocess.run(
        [
            "uv",
            "pip",
            "compile",
            "-",
            "--python-version",
            python,
            "--no-header",
            "--no-annotate",
            "--quiet",
        ],
        input=spec + "\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    pinned: list[tuple[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("--"):
            continue
        # uv emits "name==version" (markers/extras already resolved away)
        if "==" not in line:
            continue
        name, _, version = line.partition("==")
        name = name.strip()
        version = version.split(";")[0].strip().split(" ")[0]
        if name.replace("_", "-").lower() == root_name:
            continue
        pinned.append((name, version))
    return pinned


def sdist(name: str, version: str) -> tuple[str, str, str]:
    """Return (canonical_name, sdist_url, sha256) from PyPI for an exact version."""
    endpoint = PYPI.format(name=name, version=version)
    with urllib.request.urlopen(endpoint, timeout=PYPI_TIMEOUT) as r:  # noqa: S310
        data = json.load(r)
    canonical = data["info"]["name"]
    for f in data["urls"]:
        if f["packagetype"] == "sdist":
            return canonical, f["url"], f["digests"]["sha256"]
    raise SystemExit(
        f"!! no sdist on PyPI for {name}=={version} "
        "(wheel-only) — formula would not build from source"
    )


def main() -> None:
    spec = sys.argv[1]
    python = "3.13"
    if "--python" in sys.argv:
        python = sys.argv[sys.argv.index("--python") + 1]

    resolved = resolve(spec, python)
    blocks: list[tuple[str, str]] = []
    for name, version in resolved:
        canonical, url, sha = sdist(name, version)
        block = f'  resource "{canonical}" do\n    url "{url}"\n    sha256 "{sha}"\n  end'
        blocks.append((canonical.lower(), block))

    for _, block in sorted(blocks):
        print(block)
        print()

    print(f"# {len(blocks)} resources resolved for {spec} on python {python}", file=sys.stderr)


if __name__ == "__main__":
    main()
