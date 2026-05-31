"""Minimal HTTP incident + vault viewer.

Read-only browser for ``.autosentry/incidents/`` and (0.12.0)
``.autosentry/vault/``. Backed by stdlib ``http.server`` so there's
no Flask/FastAPI dependency to drag in.

Routes:

- ``GET /``                        — redirects to ``/incidents``
- ``GET /incidents``               — list (newest first) from ``index.jsonl``
- ``GET /incidents/<id>``          — full incident report (Markdown → HTML)
- ``GET /incidents/<id>/raw/<f>``  — serve raw artifacts (trace.txt, frames,
                                     configs, state.json) within the folder
- ``GET /vault``                   — vault home (linked categories)
- ``GET /vault/<subdir>/<file>``   — render a vault note, resolving
                                     ``[[wikilinks]]`` to ``/vault/...`` URLs
- ``GET /vault/graph``             — mermaid graph of runs → incidents →
                                     attempts → outcomes
- ``GET /healthz``                 — server liveness probe

The renderer uses ``markdown-it-py`` which we already get transitively
through ``rich``; we pin it explicitly in pyproject.toml.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from markdown_it import MarkdownIt

# ----- data layer ----------------------------------------------------------


@dataclass(frozen=True)
class IncidentSummary:
    id: str
    kind: str
    detector: str
    rule: str | None
    action: str | None
    message: str


def read_index(incidents_dir: Path) -> list[IncidentSummary]:
    """Read ``index.jsonl`` (newest first)."""
    idx = incidents_dir / "index.jsonl"
    if not idx.exists():
        return []
    out: list[IncidentSummary] = []
    with idx.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.append(
                IncidentSummary(
                    id=raw.get("id", ""),
                    kind=raw.get("kind", ""),
                    detector=raw.get("detector", ""),
                    rule=raw.get("rule"),
                    action=raw.get("action"),
                    message=raw.get("message", ""),
                )
            )
    out.reverse()  # newest first
    return out


_INCIDENT_ID_RE = re.compile(r"^[A-Za-z0-9._\-:]+$")


def _is_safe_id(incident_id: str) -> bool:
    """Defense against path-traversal in URL-supplied incident ids."""
    return bool(_INCIDENT_ID_RE.match(incident_id))


# ----- HTML rendering ------------------------------------------------------

_MD = MarkdownIt("commonmark", {"breaks": True, "html": False, "linkify": True})

_PAGE_CSS = """
:root { color-scheme: light dark; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               Helvetica, Arial, sans-serif;
  max-width: 1100px; margin: 2rem auto; padding: 0 1rem; line-height: 1.55;
}
h1, h2, h3 { line-height: 1.2; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #4443; }
th { background: #8881; }
.tag { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 4px;
       font-size: 0.85rem; background: #8883; }
.tag.error   { background: #ef555533; color: #ef5555; }
.tag.anomaly { background: #f2c25533; color: #f2c255; }
.tag.exit    { background: #aaaaaa33; color: #777; }
pre, code { font-family: "SFMono-Regular", Menlo, monospace; }
pre { background: #8881; padding: 0.8rem; border-radius: 6px; overflow-x: auto; }
code { background: #8881; padding: 0.05rem 0.3rem; border-radius: 3px; }
.toolbar { display: flex; gap: 0.5rem; margin-bottom: 1rem; align-items: center; }
.toolbar input[type=search] {
  padding: 0.3rem 0.5rem; font-size: 0.95rem;
  border: 1px solid #8884; border-radius: 4px; min-width: 16rem;
  background: transparent; color: inherit;
}
.muted { color: #6669; }
nav.bread { font-size: 0.9rem; margin-bottom: 1rem; }
a { color: #3a82f7; text-decoration: none; }
a:hover { text-decoration: underline; }
ul.artifacts { list-style: none; padding: 0; }
ul.artifacts li { padding: 0.2rem 0; }
"""


def _wrap_page(title: str, body_html: str) -> str:
    return (
        "<!doctype html>\n"
        "<html lang=en>\n<head>\n"
        f"<meta charset=utf-8>\n<title>{html.escape(title)}</title>\n"
        f"<style>{_PAGE_CSS}</style>\n"
        "</head>\n<body>\n"
        f"{body_html}\n"
        "</body>\n</html>\n"
    )


def render_index_html(incidents: list[IncidentSummary]) -> str:
    rows: list[str] = []
    for inc in incidents:
        tag_kind = inc.kind if inc.kind in ("error", "anomaly", "exit") else "exit"
        rows.append(
            "<tr>"
            f"<td><a href='/incidents/{html.escape(inc.id)}'>"
            f"{html.escape(inc.id)}</a></td>"
            f"<td><span class='tag {tag_kind}'>{html.escape(inc.kind or '')}</span></td>"
            f"<td><code>{html.escape(inc.detector or '')}</code></td>"
            f"<td>{html.escape(inc.rule or '-')}</td>"
            f"<td>{html.escape(inc.action or '-')}</td>"
            f"<td>{html.escape((inc.message or '')[:120])}</td>"
            "</tr>"
        )
    if not rows:
        rows = ["<tr><td colspan=6 class=muted>no incidents yet</td></tr>"]
    body = (
        "<h1>autosentry incidents</h1>\n"
        "<nav class=bread>"
        f"<a href='/incidents'>incidents ({len(incidents)})</a>"
        "</nav>\n"
        "<div class=toolbar>"
        "<input id=q type=search placeholder='filter…' "
        "oninput='filterRows(this.value)'>"
        "<span class=muted>local-only viewer · read-only</span>"
        "</div>\n"
        "<table id=incidents>\n"
        "<thead><tr><th>id</th><th>kind</th><th>detector</th>"
        "<th>rule</th><th>action</th><th>message</th></tr></thead>\n"
        "<tbody>\n" + "\n".join(rows) + "\n</tbody>\n</table>\n"
        "<script>\n"
        "function filterRows(q) {\n"
        "  q = q.toLowerCase();\n"
        "  document.querySelectorAll('#incidents tbody tr').forEach(tr => {\n"
        "    tr.style.display = tr.innerText.toLowerCase().includes(q) ? '' : 'none';\n"
        "  });\n"
        "}\n"
        "</script>\n"
    )
    return _wrap_page("autosentry incidents", body)


# ----- vault rendering -----------------------------------------------------

_VAULT_SUBDIRS = ("runs", "incidents", "detectors", "patterns", "regressions", "exhaustions")
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]")


def _resolve_wikilink(vault_root: Path, target: str) -> str | None:
    """Find which vault subdir contains ``<target>.md`` and return a
    server-relative URL. Returns None if no match — the renderer will
    fall back to plain text.

    Two layout cases:

    1. Top-level note: ``vault/<sub>/<target>.md`` (URL same as path).
    2. Nested note: the wikilink ``<parent>-<child>`` maps to
       ``vault/<sub>/<parent>/<child>.md`` (URL strips the parent prefix
       from the path tail). E.g. ``run-1-child-1`` is the wikilink id;
       the file is at ``runs/run-1/child-1.md``.
    """
    target = target.strip()
    if not target:
        return None
    for sub in _VAULT_SUBDIRS:
        # Case 1: top-level note.
        if (vault_root / sub / f"{target}.md").exists():
            return f"/vault/{sub}/{target}"
        # Case 2: nested note. Walk each parent dir in this sub and
        # try matching the wikilink target as ``<parent>-<child>``.
        sub_dir = vault_root / sub
        if not sub_dir.is_dir():
            continue
        for parent_dir in sub_dir.iterdir():
            if not parent_dir.is_dir():
                continue
            prefix = parent_dir.name + "-"
            if target.startswith(prefix):
                child = target[len(prefix) :]
                if (parent_dir / f"{child}.md").exists():
                    return f"/vault/{sub}/{parent_dir.name}/{child}"
    return None


def _rewrite_wikilinks(body: str, vault_root: Path) -> str:
    """Replace ``[[note]]`` and ``[[note|label]]`` with markdown links
    pointing at the resolved vault URL. Unresolved links stay as plain
    text so the rendered page doesn't dangle on missing targets."""

    def repl(m: re.Match[str]) -> str:
        target = m.group(1)
        label = m.group(2) or target
        url = _resolve_wikilink(vault_root, target)
        if url is None:
            return f"<code>{html.escape(label)}</code>"
        return f"[{label}]({url})"

    return _WIKILINK_RE.sub(repl, body)


def render_vault_note_html(vault_root: Path, *, subpath: str, body_md: str) -> str:
    """Render a single vault note. ``subpath`` is the URL path under
    ``/vault/`` (e.g. ``incidents/inc-1`` or ``runs/foo/child-1``)."""
    body_md = _rewrite_wikilinks(body_md, vault_root)
    rendered = _MD.render(body_md)
    body = (
        "<nav class=bread>"
        "<a href='/vault'>← vault</a> / "
        f"<code>{html.escape(subpath)}</code>"
        " · <a href='/vault/graph'>graph view</a>"
        "</nav>\n" + rendered
    )
    return _wrap_page(f"vault: {subpath}", body)


def render_vault_index_html(vault_root: Path) -> str:
    """Vault home page — a categorized link list of every note. Cheap
    to recompute on each request (vault is small)."""
    sections: list[str] = []
    for sub in _VAULT_SUBDIRS:
        d = vault_root / sub
        if not d.is_dir():
            continue
        items: list[str] = []
        for note in sorted(d.glob("*.md")):
            name = note.stem
            items.append(f"<li><a href='/vault/{sub}/{name}'>{html.escape(name)}</a></li>")
        # Also surface nested notes (attempts, child runs).
        for parent_dir in sorted(d.glob("*/")):
            for note in sorted(parent_dir.glob("*.md")):
                rel = f"{parent_dir.name}/{note.stem}"
                items.append(
                    f"<li class=muted style='margin-left:1rem'>↳ "
                    f"<a href='/vault/{sub}/{rel}'>{html.escape(rel)}</a></li>"
                )
        if not items:
            continue
        sections.append(
            f"<h2>{sub} <span class=muted>({len(items)})</span></h2>\n"
            "<ul class=artifacts>\n" + "\n".join(items) + "\n</ul>"
        )
    if not sections:
        sections = ["<p class=muted>vault is empty — autosentry hasn't run yet.</p>"]
    body = (
        "<h1>autosentry vault</h1>\n"
        "<nav class=bread>"
        "<a href='/incidents'>incidents</a> · "
        "<a href='/vault'>vault</a> · "
        "<a href='/vault/graph'>graph view</a>"
        "</nav>\n" + "\n".join(sections)
    )
    return _wrap_page("autosentry vault", body)


def render_vault_graph_html(vault_root: Path, incidents_dir: Path) -> str:
    """Mermaid graph of run → incident → attempt → outcome chains.

    The graph is generated by walking the existing on-disk data
    (``incidents/index.jsonl`` and the vault directory) rather than
    parsing every markdown note — that's faster and survives
    template tweaks.
    """
    mermaid = _build_mermaid_graph(vault_root, incidents_dir)
    body = (
        "<h1>autosentry vault graph</h1>\n"
        "<nav class=bread>"
        "<a href='/vault'>← vault</a> · "
        "<a href='/incidents'>incidents</a>"
        "</nav>\n"
        "<p class=muted>nodes link back to their vault notes; click to drill in.</p>\n"
        "<div class='mermaid'>\n" + mermaid + "\n</div>\n"
        "<script src='https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js'></script>\n"
        "<script>mermaid.initialize({ startOnLoad: true, securityLevel: 'loose' });</script>\n"
    )
    return _wrap_page("autosentry vault graph", body)


def _build_mermaid_graph(vault_root: Path, incidents_dir: Path) -> str:
    """Walk vault + incidents/index.jsonl and emit a ``graph TD``
    diagram. Each node carries a ``click <id> "url"`` directive so
    users can navigate from the graph view back into a note."""
    lines: list[str] = ["graph TD"]
    node_ids: dict[str, str] = {}

    def node(key: str, label: str, url: str | None = None) -> str:
        if key in node_ids:
            return node_ids[key]
        nid = f"n{len(node_ids)}"
        node_ids[key] = nid
        safe_label = label.replace('"', "'")[:60]
        lines.append(f'  {nid}["{safe_label}"]')
        if url:
            lines.append(f'  click {nid} "{url}"')
        return nid

    # Runs and their child restarts.
    runs_dir = vault_root / "runs"
    if runs_dir.is_dir():
        for run_note in sorted(runs_dir.glob("*.md")):
            run_id = run_note.stem
            run_n = node(f"run:{run_id}", f"run {run_id[:20]}", f"/vault/runs/{run_id}")
            children_dir = runs_dir / run_id
            if children_dir.is_dir():
                for child_note in sorted(children_dir.glob("*.md")):
                    cid = f"{run_id}-{child_note.stem}"
                    child_n = node(
                        f"child:{cid}",
                        f"child {child_note.stem}",
                        f"/vault/runs/{run_id}/{child_note.stem}",
                    )
                    lines.append(f"  {run_n} --> {child_n}")

    # Incidents → attempts. Source of truth: index.jsonl.
    idx_path = incidents_dir / "index.jsonl"
    if idx_path.exists():
        with idx_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                inc_id = entry.get("id")
                if not inc_id:
                    continue
                inc_n = node(
                    f"incident:{inc_id}",
                    f"{entry.get('detector', '?')} · {entry.get('kind', '?')}",
                    f"/vault/incidents/{inc_id}",
                )
                attempts_dir = vault_root / "incidents" / inc_id
                if attempts_dir.is_dir():
                    for attempt_note in sorted(attempts_dir.glob("attempt-*.md")):
                        attempt_n = node(
                            f"attempt:{inc_id}-{attempt_note.stem}",
                            attempt_note.stem,
                            f"/vault/incidents/{inc_id}/{attempt_note.stem}",
                        )
                        lines.append(f"  {inc_n} --> {attempt_n}")
                # Patterns connect across incidents.
                rule = entry.get("rule")
                if rule:
                    rule_n = node(f"rule:{rule}", f"rule {rule}", url=None)
                    lines.append(f"  {inc_n} -.-> {rule_n}")

    # Patterns aggregate multiple incidents.
    patterns_dir = vault_root / "patterns"
    if patterns_dir.is_dir():
        for p_note in sorted(patterns_dir.glob("pattern-*.md")):
            slug = p_note.stem
            pat_n = node(f"pattern:{slug}", slug, f"/vault/patterns/{slug}")
            try:
                body = p_note.read_text(encoding="utf-8")
            except OSError:
                continue
            for m in re.finditer(r"\[\[([^\]|]+?)\]\]", body):
                target = m.group(1)
                inc_n = node_ids.get(f"incident:{target}")
                if inc_n is not None:
                    lines.append(f"  {pat_n} -.-> {inc_n}")

    if len(lines) == 1:
        lines.append('  empty["vault is empty — autosentry hasn\'t run yet"]')
    return "\n".join(lines)


def render_incident_html(incident_id: str, *, report_md: str, artifacts: list[Path]) -> str:
    body_md = _MD.render(report_md)
    artifact_items = []
    for p in artifacts:
        href = f"/incidents/{html.escape(incident_id)}/raw/{html.escape(p.name)}"
        artifact_items.append(f"<li><a href='{href}'>{html.escape(p.name)}</a></li>")
    artifacts_block = (
        f"<h2>artifacts</h2>\n<ul class=artifacts>\n{''.join(artifact_items)}\n</ul>\n"
        if artifact_items
        else ""
    )
    body = (
        "<nav class=bread>"
        "<a href='/incidents'>← incidents</a>"
        f" / <code>{html.escape(incident_id)}</code>"
        "</nav>\n" + body_md + "\n" + artifacts_block
    )
    return _wrap_page(f"incident {incident_id}", body)


# ----- HTTP server ---------------------------------------------------------


class IncidentServer:
    """The state the request handler needs at request time."""

    def __init__(self, incidents_dir: Path, vault_root: Path | None = None) -> None:
        self.incidents_dir = incidents_dir
        # ``vault_root`` is optional so older callers (and the test
        # suite) don't have to wire it up. Vault routes silently 404
        # when it's None.
        self.vault_root = vault_root

    def list_incidents(self) -> list[IncidentSummary]:
        return read_index(self.incidents_dir)

    def incident_folder(self, incident_id: str) -> Path | None:
        if not _is_safe_id(incident_id):
            return None
        folder = self.incidents_dir / incident_id
        if not folder.is_dir():
            return None
        return folder

    def list_artifacts(self, incident_id: str) -> list[Path]:
        folder = self.incident_folder(incident_id)
        if folder is None:
            return []
        # Top-level artifacts, plus a couple of well-known children.
        items: list[Path] = []
        for name in (
            "trace.txt",
            "log_excerpt.txt",
            "state.json",
            "rule_match.json",
        ):
            p = folder / name
            if p.exists():
                items.append(p)
        for sub in ("frames", "configs", "fix"):
            p = folder / sub
            if p.is_dir():
                for child in sorted(p.iterdir()):
                    items.append(child)
        return items


def build_app(
    incidents_dir: Path,
    vault_root: Path | None = None,
) -> type[_Handler]:
    """Return a handler class bound to the given incidents directory.

    ``vault_root`` is optional — pass it to enable the vault routes.
    """
    server = IncidentServer(incidents_dir=incidents_dir, vault_root=vault_root)

    class _BoundHandler(_Handler):
        viewer = server

    return _BoundHandler


class _Handler(BaseHTTPRequestHandler):
    viewer: IncidentServer  # set by build_app

    # ----- routing ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 — http.server convention
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path in ("", "/"):
            self._redirect("/incidents")
            return
        if path == "/healthz":
            self._respond(200, "ok", content_type="text/plain")
            return
        if path == "/incidents":
            self._handle_index()
            return
        m = re.match(r"^/incidents/([^/]+)$", path)
        if m:
            self._handle_show(m.group(1))
            return
        m = re.match(r"^/incidents/([^/]+)/raw/(.+)$", path)
        if m:
            self._handle_raw(m.group(1), m.group(2))
            return
        if path == "/vault" or path == "/vault/":
            self._handle_vault_index()
            return
        if path == "/vault/graph":
            self._handle_vault_graph()
            return
        m = re.match(r"^/vault/([a-z]+)/(.+)$", path)
        if m:
            self._handle_vault_note(m.group(1), m.group(2))
            return
        self._respond(404, "not found", content_type="text/plain")

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Quieter default logger; tests would otherwise spam stderr.
        return

    # ----- handlers --------------------------------------------------------

    def _handle_index(self) -> None:
        incidents = self.viewer.list_incidents()
        self._respond(200, render_index_html(incidents))

    def _handle_show(self, incident_id: str) -> None:
        folder = self.viewer.incident_folder(incident_id)
        if folder is None:
            self._respond(404, "incident not found", content_type="text/plain")
            return
        report = folder / "report.md"
        if not report.exists():
            self._respond(404, f"no report.md in {incident_id}", content_type="text/plain")
            return
        text = report.read_text(encoding="utf-8", errors="replace")
        artifacts = self.viewer.list_artifacts(incident_id)
        self._respond(
            200,
            render_incident_html(incident_id, report_md=text, artifacts=artifacts),
        )

    # ----- vault routes ----------------------------------------------------

    def _handle_vault_index(self) -> None:
        if self.viewer.vault_root is None or not self.viewer.vault_root.is_dir():
            self._respond(404, "vault not configured", content_type="text/plain")
            return
        self._respond(200, render_vault_index_html(self.viewer.vault_root))

    def _handle_vault_note(self, subdir: str, rel: str) -> None:
        if self.viewer.vault_root is None or not self.viewer.vault_root.is_dir():
            self._respond(404, "vault not configured", content_type="text/plain")
            return
        if subdir not in _VAULT_SUBDIRS:
            self._respond(404, "unknown vault section", content_type="text/plain")
            return
        if not _is_safe_id(rel.replace("/", "-").rstrip(".md")):
            self._respond(400, "bad path", content_type="text/plain")
            return
        # Allow ``<parent>/<child>`` (e.g. attempt notes); reject deeper.
        if rel.count("/") > 1:
            self._respond(400, "bad path", content_type="text/plain")
            return
        candidate = self.viewer.vault_root / subdir / f"{rel}.md"
        try:
            candidate = candidate.resolve()
            candidate.relative_to(self.viewer.vault_root.resolve())
        except (OSError, ValueError):
            self._respond(400, "bad path", content_type="text/plain")
            return
        if not candidate.is_file():
            self._respond(404, "vault note not found", content_type="text/plain")
            return
        body_md = candidate.read_text(encoding="utf-8", errors="replace")
        # Strip frontmatter for rendering — Obsidian eats it but
        # markdown-it would render it as a horizontal rule + indented list.
        body_md = _strip_frontmatter(body_md)
        self._respond(
            200,
            render_vault_note_html(
                self.viewer.vault_root, subpath=f"{subdir}/{rel}", body_md=body_md
            ),
        )

    def _handle_vault_graph(self) -> None:
        if self.viewer.vault_root is None or not self.viewer.vault_root.is_dir():
            self._respond(404, "vault not configured", content_type="text/plain")
            return
        self._respond(
            200,
            render_vault_graph_html(self.viewer.vault_root, self.viewer.incidents_dir),
        )

    def _handle_raw(self, incident_id: str, rel: str) -> None:
        folder = self.viewer.incident_folder(incident_id)
        if folder is None:
            self._respond(404, "incident not found", content_type="text/plain")
            return
        # Resolve and validate that the target stays inside the folder.
        target = (folder / rel).resolve()
        try:
            target.relative_to(folder.resolve())
        except ValueError:
            self._respond(400, "bad path", content_type="text/plain")
            return
        if not target.is_file():
            self._respond(404, "not found", content_type="text/plain")
            return
        body = target.read_bytes()
        content_type = _guess_content_type(target.name)
        self._respond(200, body, content_type=content_type)

    # ----- low-level helpers ----------------------------------------------

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _respond(
        self,
        code: int,
        body: str | bytes,
        *,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)


_FRONTMATTER_BLOCK_RE = re.compile(r"^---\n.*?\n---\n+", re.DOTALL)


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter from a vault note for HTML rendering.

    Obsidian renders frontmatter as a discreet metadata panel; markdown-it
    would treat it as a horizontal rule plus an indented list. Easier to
    strip it for the web view and render the body alone.
    """
    return _FRONTMATTER_BLOCK_RE.sub("", text, count=1)


def _guess_content_type(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".json"):
        return "application/json"
    if lower.endswith(".md"):
        return "text/markdown; charset=utf-8"
    if lower.endswith((".py", ".sh", ".yaml", ".yml", ".toml", ".ini", ".txt", ".log")):
        return "text/plain; charset=utf-8"
    if lower.endswith(".patch") or lower.endswith(".diff"):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


# ----- entry point --------------------------------------------------------


def run_server(
    incidents_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    vault_root: Path | None = None,
) -> None:
    """Block running an :class:`http.server.ThreadingHTTPServer`."""
    handler = build_app(incidents_dir, vault_root=vault_root)
    with ThreadingHTTPServer((host, port), handler) as httpd:
        print(f"autosentry web · http://{host}:{port}/incidents")
        if vault_root is not None and vault_root.is_dir():
            print(f"             · http://{host}:{port}/vault")
            print(f"             · http://{host}:{port}/vault/graph")
        if host == "0.0.0.0":  # noqa: S104 — explicit warning printed
            print("warning: binding to 0.0.0.0 — incident contents are world-readable on your LAN")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
