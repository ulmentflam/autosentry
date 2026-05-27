"""Multi-language traceback detector.

Picks up stack traces from the log stream by recognizing language-specific
"start of trace" patterns and then accumulating subsequent lines until
the trace ends. Supports:

- Python: ``Traceback (most recent call last):``
- Node.js / JavaScript: ``Error: …`` followed by ``    at file:line:col`` frames
- Go: ``panic: …`` followed by goroutine stack frames
- Rust: ``thread '...' panicked at`` + backtrace
- Java: ``Exception in thread`` + ``\tat …``

The captured trace is included verbatim in the :class:`Detection`. The
incident store's source exploder later re-parses it to extract
``(file, line, lang)`` triples.
"""

from __future__ import annotations

import re
from collections import deque

from autosentry.detectors.base import Detection, Detector
from autosentry.supervisors.base import LogLine

_CTX = 100
# Max lines we'll accumulate into one trace before forcing a flush.
_MAX_TRACE_LINES = 500


class _TraceState:
    def __init__(self, lang: str, header: str) -> None:
        self.lang = lang
        self.lines: list[str] = [header]
        # Number of lines we've seen since the last "stack-y" line; if this
        # crosses a threshold we assume the trace is over.
        self.quiet_lines = 0


_PY_START = re.compile(r"^\s*Traceback \(most recent call last\):\s*$")
_PY_FRAME = re.compile(r'^\s*File "[^"]+", line \d+(, in .+)?\s*$')
_PY_END = re.compile(r"^\s*([A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt))\b.*$")

_NODE_HEADER = re.compile(r"^\s*[A-Za-z_][\w.]*(?:Error|Exception)\b.*$")
_NODE_FRAME = re.compile(r"^\s*at .+ \(.+:\d+:\d+\)\s*$")
_NODE_FRAME_NOPAREN = re.compile(r"^\s*at .+:\d+:\d+\s*$")

_GO_HEADER = re.compile(r"^\s*panic: .+")
_GO_FRAME = re.compile(r"^\s*\S+\.go:\d+( \+0x[0-9a-f]+)?\s*$")

_RUST_HEADER = re.compile(r"^\s*thread '.+' panicked at .+")
_RUST_FRAME = re.compile(r"^\s*\d+:\s+0x[0-9a-f]+\s+-\s+.+")

_JAVA_HEADER = re.compile(r"^\s*Exception in thread .+|^\s*Caused by: .+")
_JAVA_FRAME = re.compile(r"^\s*at [\w.$<>]+\(.+:\d+\)\s*$")


def _starts_trace(text: str) -> tuple[str, str] | None:
    """Return (lang, normalized_header) if this line opens a trace."""
    if _PY_START.match(text):
        return ("python", text)
    if _GO_HEADER.match(text):
        return ("go", text)
    if _RUST_HEADER.match(text):
        return ("rust", text)
    if _JAVA_HEADER.match(text):
        return ("java", text)
    # Node tracebacks open with the error line. We only commit if the
    # *next* line looks like a node frame — handled in observe_line by
    # using a one-line lookahead via the active state machine.
    return None


def _is_frame(lang: str, text: str) -> bool:
    if lang == "python":
        return bool(_PY_FRAME.match(text) or text.lstrip().startswith(" "))
    if lang == "javascript":
        return bool(_NODE_FRAME.match(text) or _NODE_FRAME_NOPAREN.match(text))
    if lang == "go":
        return bool(_GO_FRAME.match(text)) or text.lstrip().startswith("goroutine ")
    if lang == "rust":
        return bool(_RUST_FRAME.match(text))
    if lang == "java":
        return bool(_JAVA_FRAME.match(text)) or text.lstrip().startswith("Caused by:")
    return False


class TracebackDetector(Detector):
    def __init__(self, *, name: str = "traceback", cooldown_seconds: int = 30) -> None:
        super().__init__(name=name, cooldown_seconds=cooldown_seconds)
        self._buf: deque[str] = deque(maxlen=_CTX)
        self._state: _TraceState | None = None
        # Node detection needs a one-line lookback; remember last line.
        self._prev_line: str | None = None

    def observe_line(self, line: LogLine) -> Detection | None:
        text = line.text
        self._buf.append(text)

        if self._state is None:
            return self._maybe_open(text)

        # We're inside a trace — keep collecting until it ends.
        if len(self._state.lines) >= _MAX_TRACE_LINES:
            return self._close_trace()

        if _is_frame(self._state.lang, text) or (
            self._state.lang == "python" and _PY_END.match(text)
        ):
            self._state.lines.append(text)
            self._state.quiet_lines = 0
            # Python traces end on the final exception line — flush immediately
            # so we don't swallow downstream log noise.
            if self._state.lang == "python" and _PY_END.match(text):
                return self._close_trace()
            return None

        # Not a frame. Allow one blank/continuation line, then close.
        self._state.quiet_lines += 1
        if self._state.quiet_lines >= 2 or text.strip() == "":
            return self._close_trace()
        # Otherwise it's still part of the trace's body (e.g., multi-line
        # exception messages).
        self._state.lines.append(text)
        return None

    def _maybe_open(self, text: str) -> Detection | None:
        opened = _starts_trace(text)
        if opened:
            lang, header = opened
            self._state = _TraceState(lang=lang, header=header)
            self._prev_line = text
            return None
        # Node: error line followed by an "at …" frame.
        if (
            self._prev_line
            and _NODE_HEADER.match(self._prev_line)
            and (_NODE_FRAME.match(text) or _NODE_FRAME_NOPAREN.match(text))
        ):
            self._state = _TraceState(lang="javascript", header=self._prev_line)
            self._state.lines.append(text)
            self._prev_line = text
            return None
        self._prev_line = text
        return None

    def _close_trace(self) -> Detection | None:
        if self._state is None:
            return None
        if self._on_cooldown():
            self._state = None
            return None
        self._mark_fired()
        trace_text = "\n".join(self._state.lines)
        lang = self._state.lang
        header = self._state.lines[0].strip()
        self._state = None
        return Detection(
            detector=self.name,
            kind="error",
            message=f"{lang} traceback: {header[:200]}",
            log_context=list(self._buf),
            trace=trace_text,
            meta={"lang": lang},
        )
