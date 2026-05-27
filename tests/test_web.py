"""Web viewer tests.

We don't bind a real socket; we exercise the renderer + the
``IncidentServer`` data layer, then spin a server up briefly to confirm
routing for the index and one incident detail.
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
    IncidentServer,
    _is_safe_id,
    build_app,
    read_index,
    render_incident_html,
    render_index_html,
)


def _seed_incidents(tmp_path: Path) -> Path:
    """Create a tiny `.autosentry/incidents/` fixture."""
    incidents = tmp_path / "incidents"
    incidents.mkdir()
    idx = incidents / "index.jsonl"
    idx.write_text(
        json.dumps(
            {
                "id": "2026-05-26T14-32-10Z-error-traceback",
                "kind": "error",
                "detector": "traceback",
                "rule": "oom_halve_batch",
                "action": "restart_with_env",
                "message": "OOM at step 42",
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "2026-05-26T18-04-55Z-anomaly-stall",
                "kind": "anomaly",
                "detector": "training_stall",
                "rule": None,
                "action": None,
                "message": "no progress on step 8450 for 31m",
            }
        )
        + "\n"
    )
    folder = incidents / "2026-05-26T14-32-10Z-error-traceback"
    folder.mkdir()
    (folder / "report.md").write_text("# Incident — oom\n\n**Resolution:** restart_with_env\n")
    (folder / "trace.txt").write_text("Traceback…\n")
    (folder / "state.json").write_text("{}")
    return incidents


# ----- pure data + rendering ----------------------------------------------


def test_read_index_newest_first(tmp_path: Path):
    incidents_dir = _seed_incidents(tmp_path)
    items = read_index(incidents_dir)
    assert [i.id for i in items] == [
        "2026-05-26T18-04-55Z-anomaly-stall",
        "2026-05-26T14-32-10Z-error-traceback",
    ]


def test_is_safe_id_blocks_traversal():
    assert _is_safe_id("2026-05-26T14-32-10Z-error-traceback")
    assert not _is_safe_id("../etc/passwd")
    assert not _is_safe_id("foo/bar")
    assert not _is_safe_id("")


def test_render_index_html_includes_links_and_filter(tmp_path: Path):
    incidents_dir = _seed_incidents(tmp_path)
    items = read_index(incidents_dir)
    html = render_index_html(items)
    assert "<table id=incidents>" in html
    assert "/incidents/2026-05-26T14-32-10Z-error-traceback" in html
    assert "filterRows" in html  # the search field is wired


def test_render_index_html_empty_state():
    html = render_index_html([])
    assert "no incidents yet" in html


def test_render_incident_html_includes_artifacts(tmp_path: Path):
    incidents_dir = _seed_incidents(tmp_path)
    server = IncidentServer(incidents_dir=incidents_dir)
    incident_id = "2026-05-26T14-32-10Z-error-traceback"
    artifacts = server.list_artifacts(incident_id)
    folder = server.incident_folder(incident_id)
    assert folder is not None
    md = (folder / "report.md").read_text()
    html = render_incident_html(incident_id, report_md=md, artifacts=artifacts)
    assert "<h1>" in html
    assert "trace.txt" in html
    assert "state.json" in html


def test_incident_folder_rejects_unsafe_id(tmp_path: Path):
    incidents_dir = _seed_incidents(tmp_path)
    server = IncidentServer(incidents_dir=incidents_dir)
    assert server.incident_folder("../etc/passwd") is None
    assert server.incident_folder("doesnt-exist") is None


# ----- HTTP integration ----------------------------------------------------


@pytest.fixture
def running_server(tmp_path: Path):
    incidents_dir = _seed_incidents(tmp_path)
    handler = build_app(incidents_dir)
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


def test_http_index_returns_html(running_server: int):
    code, body = _get(running_server, "/incidents")
    assert code == 200
    assert "<table id=incidents>" in body
    assert "2026-05-26T18-04-55Z-anomaly-stall" in body


def test_http_root_redirects(running_server: int):
    conn = http.client.HTTPConnection("127.0.0.1", running_server, timeout=5)
    conn.request("GET", "/")
    resp = conn.getresponse()
    assert resp.status == 302
    assert resp.getheader("Location") == "/incidents"


def test_http_incident_show(running_server: int):
    code, body = _get(running_server, "/incidents/2026-05-26T14-32-10Z-error-traceback")
    assert code == 200
    assert "Incident — oom" in body
    assert "trace.txt" in body


def test_http_raw_artifact(running_server: int):
    code, body = _get(
        running_server,
        "/incidents/2026-05-26T14-32-10Z-error-traceback/raw/trace.txt",
    )
    assert code == 200
    assert body == "Traceback…\n"


def test_http_path_traversal_blocked(running_server: int):
    code, _ = _get(running_server, "/incidents/..%2Fetc%2Fpasswd")
    assert code == 404


def test_http_healthz(running_server: int):
    code, body = _get(running_server, "/healthz")
    assert code == 200
    assert body == "ok"


def test_http_unknown_path_404(running_server: int):
    code, _ = _get(running_server, "/totally-not-real")
    assert code == 404


# Cosmetic: make sure markdown-it picks up the dep without importing rich's
# transitive copy when our pyproject pins it.
def test_markdown_import():
    from markdown_it import MarkdownIt

    md = MarkdownIt()
    out = md.render("# hi\n")
    assert "<h1>hi</h1>" in out


# Ensure tests don't leave threads dangling — give the server a moment to shut.
def teardown_module(module):  # noqa: ARG001
    time.sleep(0.05)
