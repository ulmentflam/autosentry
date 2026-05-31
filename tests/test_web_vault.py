"""Tests for the vault routes added in 0.12.0.

Pattern mirrors ``tests/test_web.py``: spin a `ThreadingHTTPServer` on
an ephemeral port, hit it with ``http.client``, assert status + body
substrings. No real LLM calls; vault notes are seeded directly.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from autosentry.web.server import (
    _resolve_wikilink,
    _rewrite_wikilinks,
    _strip_frontmatter,
    build_app,
    render_vault_graph_html,
)

# ---------------------------------------------------------------------------
# Test vault scaffolding
# ---------------------------------------------------------------------------


def _seed_vault(tmp_path: Path) -> tuple[Path, Path]:
    """Seed a tiny `.autosentry/vault/` + matching incidents/index.jsonl
    so the graph view has data to render. Returns (incidents_dir,
    vault_root)."""
    autosentry = tmp_path / ".autosentry"
    vault = autosentry / "vault"
    incidents = autosentry / "incidents"
    for sub in ("runs", "incidents", "detectors", "patterns", "regressions", "exhaustions"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    incidents.mkdir(parents=True, exist_ok=True)

    (vault / "index.md").write_text("---\nid: index\nkind: index\n---\n\n# vault\n")
    (vault / "runs" / "run-1.md").write_text(
        "---\nid: run-1\nkind: run\n---\n\n"
        "# Run `run-1`\n\n"
        "## Child restarts\n- [[run-1-child-1]]\n\n"
        "## Incidents\n- [[inc-1]] — `oom`\n"
    )
    (vault / "runs" / "run-1").mkdir(exist_ok=True)
    (vault / "runs" / "run-1" / "child-1.md").write_text(
        "---\nid: run-1-child-1\nkind: child-run\n---\n\n# Child 1\n"
    )
    (vault / "incidents" / "inc-1.md").write_text(
        "---\nid: inc-1\nkind: incident\ndetector: oom\n---\n\n"
        "# Incident\n\n"
        "- **Run:** [[run-1]] -> [[run-1-child-1]]\n"
        "- **Detector:** [[detector-oom]]\n"
        "- **Pattern:** [[pattern-oom-cuda]]\n"
    )
    (vault / "incidents" / "inc-1").mkdir(exist_ok=True)
    (vault / "incidents" / "inc-1" / "attempt-1.md").write_text(
        "---\nid: inc-1-attempt-1\nkind: attempt\n---\n\n# Attempt 1\n"
    )
    (vault / "detectors" / "oom.md").write_text(
        "---\nid: detector-oom\nkind: detector\n---\n\n# oom\n"
    )
    (vault / "patterns" / "pattern-oom-cuda.md").write_text(
        "---\nid: pattern-oom-cuda\nkind: pattern\n---\n\n"
        "# Pattern\n\n## Linked incidents\n- [[inc-1]]\n"
    )

    (incidents / "index.jsonl").write_text(
        json.dumps(
            {
                "id": "inc-1",
                "kind": "error",
                "detector": "oom",
                "message": "OutOfMemoryError",
                "rule": None,
                "action": None,
            }
        )
        + "\n"
    )
    return incidents, vault


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------


def test_strip_frontmatter_removes_yaml_block(tmp_path: Path) -> None:
    raw = "---\nid: x\nkind: y\n---\n\n# Title\n\nbody\n"
    assert _strip_frontmatter(raw) == "# Title\n\nbody\n"


def test_strip_frontmatter_no_op_when_absent(tmp_path: Path) -> None:
    raw = "# Title\n\nbody\n"
    assert _strip_frontmatter(raw) == raw


def test_resolve_wikilink_finds_top_level(tmp_path: Path) -> None:
    _, vault = _seed_vault(tmp_path)
    assert _resolve_wikilink(vault, "run-1") == "/vault/runs/run-1"
    assert _resolve_wikilink(vault, "inc-1") == "/vault/incidents/inc-1"
    assert _resolve_wikilink(vault, "pattern-oom-cuda") == "/vault/patterns/pattern-oom-cuda"


def test_resolve_wikilink_finds_nested(tmp_path: Path) -> None:
    _, vault = _seed_vault(tmp_path)
    # Wikilink id includes the parent prefix; the URL/file path drops it.
    assert _resolve_wikilink(vault, "run-1-child-1") == "/vault/runs/run-1/child-1"
    assert _resolve_wikilink(vault, "inc-1-attempt-1") == "/vault/incidents/inc-1/attempt-1"


def test_resolve_wikilink_returns_none_for_missing(tmp_path: Path) -> None:
    _, vault = _seed_vault(tmp_path)
    assert _resolve_wikilink(vault, "ghost") is None


def test_rewrite_wikilinks_produces_markdown_links(tmp_path: Path) -> None:
    _, vault = _seed_vault(tmp_path)
    body = "See [[inc-1]] and [[ghost|the missing one]]."
    out = _rewrite_wikilinks(body, vault)
    assert "[inc-1](/vault/incidents/inc-1)" in out
    assert "<code>the missing one</code>" in out


def test_build_mermaid_graph_includes_run_and_incidents(tmp_path: Path) -> None:
    incidents, vault = _seed_vault(tmp_path)
    html = render_vault_graph_html(vault, incidents)
    assert "graph TD" in html
    # Run and child appear with the expected click handlers.
    assert "/vault/runs/run-1" in html
    assert "/vault/runs/run-1/child-1" in html
    # Incident node has its detector + kind in the label.
    assert "oom" in html
    # Mermaid CDN script is included.
    assert "mermaid" in html.lower()


# ---------------------------------------------------------------------------
# HTTP integration
# ---------------------------------------------------------------------------


@pytest.fixture
def running_vault_server(tmp_path: Path):
    incidents, vault = _seed_vault(tmp_path)
    handler = build_app(incidents, vault_root=vault)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _get(port: int, path: str) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    return resp.status, resp.read().decode("utf-8", errors="replace")


def test_vault_index_route(running_vault_server: int) -> None:
    code, body = _get(running_vault_server, "/vault")
    assert code == 200
    assert "autosentry vault" in body
    # Categories with their counts surface in the listing.
    assert ">runs " in body or "runs (" in body
    # Direct links to the seeded notes exist.
    assert "/vault/runs/run-1" in body
    assert "/vault/incidents/inc-1" in body


def test_vault_note_route_renders_markdown(running_vault_server: int) -> None:
    code, body = _get(running_vault_server, "/vault/incidents/inc-1")
    assert code == 200
    # Markdown was rendered to HTML.
    assert "<h1>" in body
    # Wikilinks resolved to /vault/ URLs.
    assert 'href="/vault/runs/run-1"' in body
    assert 'href="/vault/patterns/pattern-oom-cuda"' in body
    # Frontmatter was stripped.
    assert "id: inc-1\nkind:" not in body


def test_vault_nested_note_route(running_vault_server: int) -> None:
    code, body = _get(running_vault_server, "/vault/runs/run-1/child-1")
    assert code == 200
    assert "<h1>Child 1</h1>" in body or "<h1>Child" in body


def test_vault_graph_route(running_vault_server: int) -> None:
    code, body = _get(running_vault_server, "/vault/graph")
    assert code == 200
    assert "graph TD" in body
    assert "mermaid" in body.lower()


def test_vault_404_for_missing_note(running_vault_server: int) -> None:
    code, _ = _get(running_vault_server, "/vault/incidents/never-existed")
    assert code == 404


def test_vault_404_for_bad_subdir(running_vault_server: int) -> None:
    code, _ = _get(running_vault_server, "/vault/elsewhere/something")
    assert code == 404


def test_vault_path_traversal_blocked(running_vault_server: int) -> None:
    code, _ = _get(running_vault_server, "/vault/incidents/..%2F..%2Fetc%2Fpasswd")
    # The handler rejects malformed paths before touching the filesystem.
    assert code in (400, 404)


def test_vault_disabled_returns_404(tmp_path: Path) -> None:
    """When ``build_app`` is called without a vault_root, vault routes
    silently 404. Useful for tests / older callers."""
    incidents, _ = _seed_vault(tmp_path)
    handler = build_app(incidents)  # no vault_root
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code, _ = _get(port, "/vault")
        assert code == 404
        code, _ = _get(port, "/vault/graph")
        assert code == 404
    finally:
        server.shutdown()
        thread.join(timeout=2)


# Ensure tests don't leave threads dangling.
def teardown_module(module):  # noqa: ARG001
    time.sleep(0.05)
