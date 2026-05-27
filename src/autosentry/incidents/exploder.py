"""Source exploder.

Given a stack trace, extract ``(file, line, language)`` frame references
and re-read the source file to produce an "exploded" view — the
enclosing function / class plus a window of context lines around the
failing line.

Implementation: regex per language to pull frame coordinates out of the
trace text; tree-sitter (via ``tree-sitter-languages``) for finding the
enclosing function / class definition node. If tree-sitter or the
grammar isn't available, fall back to ``±context_lines`` raw source.

Frames pointing at paths inside ``skip_paths`` (e.g. site-packages, the
standard library) are emitted as "library" stubs without source.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from autosentry.logger import log

try:  # tree-sitter is optional at import time so tests can run without it
    from tree_sitter_language_pack import (
        get_parser as _ts_get_parser,  # type: ignore[import-untyped]
    )

    _TS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ts_get_parser = None  # type: ignore[assignment]
    _TS_AVAILABLE = False


@dataclass(frozen=True)
class FrameRef:
    file: str
    line: int
    lang: str
    raw: str  # the original line from the trace


@dataclass
class ExplodedFrame:
    file: str
    line: int
    lang: str
    raw: str
    # source body to display — already trimmed and ready to render
    source: str | None = None
    # function/class info from tree-sitter, when available
    enclosing: str | None = None
    # set when the frame was skipped (library / not on disk / too deep)
    skipped: str | None = None


# ---- frame parsing ---------------------------------------------------------

_PY_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(, in (?P<in>.+))?')
_NODE_FRAME_PAREN = re.compile(r"^\s*at .+? \((?P<file>[^():]+):(?P<line>\d+):\d+\)")
_NODE_FRAME_BARE = re.compile(r"^\s*at (?P<file>[^():]+):(?P<line>\d+):\d+\s*$")
_GO_FRAME = re.compile(r"^\s*(?P<file>\S+\.go):(?P<line>\d+)")
_RUST_FRAME = re.compile(r"^\s*\d+:\s+0x[0-9a-f]+\s+-\s+.+\n?\s+at (?P<file>[^:]+):(?P<line>\d+)")
_JAVA_FRAME = re.compile(r"^\s*at [\w.$<>]+\((?P<file>[^:]+):(?P<line>\d+)\)")


def parse_trace_frames(trace: str, *, lang_hint: str | None = None) -> list[FrameRef]:
    """Extract frame coordinates from a raw trace string."""
    frames: list[FrameRef] = []
    lines = trace.splitlines()
    for i, line in enumerate(lines):
        for lang, regex in (
            ("python", _PY_FRAME),
            ("javascript", _NODE_FRAME_PAREN),
            ("javascript", _NODE_FRAME_BARE),
            ("go", _GO_FRAME),
            ("java", _JAVA_FRAME),
        ):
            m = regex.search(line)
            if m:
                frames.append(
                    FrameRef(
                        file=m.group("file"),
                        line=int(m.group("line")),
                        lang=lang,
                        raw=line,
                    )
                )
                break
        else:
            # Rust frames straddle two lines: "n: 0xADDR - name\n  at file:line"
            m = re.search(r"^\s+at (?P<file>[^:]+):(?P<line>\d+)", line)
            if m and i > 0 and re.match(r"^\s*\d+:\s+0x", lines[i - 1]):
                frames.append(
                    FrameRef(
                        file=m.group("file"),
                        line=int(m.group("line")),
                        lang="rust",
                        raw=line,
                    )
                )
    if lang_hint and not any(f.lang == lang_hint for f in frames):
        # Caller hinted a language; honor it for frames we couldn't classify.
        frames = [FrameRef(file=f.file, line=f.line, lang=lang_hint, raw=f.raw) for f in frames]
    return frames


# ---- tree-sitter framing ---------------------------------------------------

# Map our language names to tree-sitter-languages' identifiers.
_TS_LANG_MAP = {
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "go": "go",
    "rust": "rust",
    "java": "java",
}

# Node types we treat as "enclosing function or class" per language.
_ENCLOSING_NODES = {
    "python": {"function_definition", "class_definition"},
    "javascript": {
        "function_declaration",
        "method_definition",
        "class_declaration",
        "arrow_function",
        "function_expression",
    },
    "typescript": {
        "function_declaration",
        "method_definition",
        "class_declaration",
        "interface_declaration",
        "arrow_function",
        "function_signature",
    },
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "rust": {"function_item", "impl_item", "struct_item", "enum_item", "trait_item"},
    "java": {"method_declaration", "constructor_declaration", "class_declaration"},
}


class SourceExploder:
    def __init__(
        self,
        *,
        context_lines: int = 10,
        skip_paths: Iterable[str] = (),
        max_frames: int = 20,
        enabled_languages: Iterable[str] = (),
    ) -> None:
        self.context_lines = context_lines
        self.skip_paths = tuple(skip_paths)
        self.max_frames = max_frames
        self.enabled = set(enabled_languages)
        self._parser_cache: dict[str, object] = {}

    # ----- public API ------------------------------------------------------

    def explode_trace(self, trace: str, *, lang_hint: str | None = None) -> list[ExplodedFrame]:
        frames = parse_trace_frames(trace, lang_hint=lang_hint)
        out: list[ExplodedFrame] = []
        for fr in frames[: self.max_frames]:
            out.append(self.explode_frame(fr))
        return out

    def explode_frame(self, frame: FrameRef) -> ExplodedFrame:
        # Skip library/third-party frames.
        if any(skip in frame.file for skip in self.skip_paths):
            return ExplodedFrame(
                file=frame.file,
                line=frame.line,
                lang=frame.lang,
                raw=frame.raw,
                skipped="library or vendored path",
            )
        path = Path(frame.file)
        if not path.is_absolute():
            # Trace paths are often relative to cwd at the moment of failure.
            # Try cwd first, then leave the resolution to the file check.
            candidate = Path.cwd() / path
            if candidate.exists():
                path = candidate
        if not path.exists():
            return ExplodedFrame(
                file=str(frame.file),
                line=frame.line,
                lang=frame.lang,
                raw=frame.raw,
                skipped="source file not on disk",
            )

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log().error(f"source explode: cannot read {path}: {e}")
            return ExplodedFrame(
                file=str(path),
                line=frame.line,
                lang=frame.lang,
                raw=frame.raw,
                skipped=f"unreadable: {e}",
            )

        # Tree-sitter framing where we have a grammar and the language is enabled.
        node_range: tuple[int, int] | None = None
        enclosing: str | None = None
        if (
            _TS_AVAILABLE
            and frame.lang in _TS_LANG_MAP
            and (not self.enabled or frame.lang in self.enabled)
        ):
            try:
                node_range, enclosing = self._enclosing_via_treesitter(
                    text=text, lang=frame.lang, line=frame.line
                )
            except Exception as e:  # noqa: BLE001
                log().error(f"tree-sitter framing failed for {frame.lang}: {e}")
                node_range = None

        if node_range is not None:
            start, end = node_range
            source = self._render_window(text, start, end, frame.line)
        else:
            source = self._render_window(
                text,
                start=max(1, frame.line - self.context_lines),
                end=frame.line + self.context_lines,
                hot=frame.line,
            )
        return ExplodedFrame(
            file=str(path),
            line=frame.line,
            lang=frame.lang,
            raw=frame.raw,
            source=source,
            enclosing=enclosing,
        )

    # ----- internals --------------------------------------------------------

    def _enclosing_via_treesitter(
        self, *, text: str, lang: str, line: int
    ) -> tuple[tuple[int, int] | None, str | None]:
        ts_lang = _TS_LANG_MAP[lang]
        parser = self._parser_cache.get(ts_lang)
        if parser is None:
            if _ts_get_parser is None:  # pragma: no cover
                return None, None
            parser = _ts_get_parser(ts_lang)
            self._parser_cache[ts_lang] = parser

        tree = parser.parse(text.encode("utf-8"))  # pyrefly: ignore[missing-attribute]
        root = tree.root_node
        target_byte_row = line - 1  # tree-sitter rows are zero-based

        # Walk down to the smallest node containing the line, then back up
        # to the nearest "enclosing" type for this language.
        enclosing_types = _ENCLOSING_NODES.get(lang, set())
        cursor = root.walk()
        best: object | None = None
        # Manual descent — tree-sitter doesn't expose "node at position" directly,
        # so we descend via the cursor.
        stack: list[object] = [root]
        while stack:
            node = stack.pop()
            start_row = node.start_point[0]  # pyrefly: ignore[missing-attribute]
            end_row = node.end_point[0]  # pyrefly: ignore[missing-attribute]
            if start_row > target_byte_row or end_row < target_byte_row:
                continue
            if node.type in enclosing_types:  # pyrefly: ignore[missing-attribute]
                best = node
            for child in node.children:  # pyrefly: ignore[missing-attribute]
                stack.append(child)
        del cursor

        if best is None:
            return None, None
        start_row = best.start_point[0] + 1  # pyrefly: ignore[missing-attribute]
        end_row = best.end_point[0] + 1  # pyrefly: ignore[missing-attribute]
        signature = self._first_line_of_node(text, best)
        return (start_row, end_row), signature

    @staticmethod
    def _first_line_of_node(text: str, node: object) -> str:
        start_byte = node.start_byte  # pyrefly: ignore[missing-attribute]
        # Just grab to the next newline from start.
        nl = text.find("\n", start_byte)
        if nl == -1:
            return text[start_byte:].strip()
        return text[start_byte:nl].strip()

    def _render_window(self, text: str, start: int, end: int, hot: int) -> str:
        lines = text.splitlines()
        start = max(1, start)
        end = min(len(lines), end)
        rendered: list[str] = []
        width = len(str(end))
        for ln in range(start, end + 1):
            marker = ">>>" if ln == hot else "   "
            content = lines[ln - 1] if ln - 1 < len(lines) else ""
            rendered.append(f"{marker} {ln:>{width}}  {content}")
        return "\n".join(rendered)
