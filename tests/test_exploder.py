"""Source exploder tests."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from autosentry.incidents.exploder import SourceExploder, parse_trace_frames


def test_parse_python_frames():
    trace = dedent(
        """\
        Traceback (most recent call last):
          File "/tmp/a.py", line 10, in foo
            bar()
          File "/tmp/b.py", line 22, in bar
            raise ValueError("nope")
        ValueError: nope
        """
    )
    frames = parse_trace_frames(trace)
    assert len(frames) == 2
    assert frames[0].file == "/tmp/a.py"
    assert frames[0].line == 10
    assert frames[0].lang == "python"
    assert frames[1].line == 22


def test_parse_node_frames():
    trace = dedent(
        """\
        TypeError: bad
            at Foo.bar (/app/src/index.js:42:7)
            at Module._compile (node:internal/modules/cjs/loader:1101:14)
        """
    )
    frames = parse_trace_frames(trace)
    js_frames = [f for f in frames if f.lang == "javascript"]
    assert any(f.file == "/app/src/index.js" and f.line == 42 for f in js_frames)


def test_explode_with_tree_sitter_or_fallback(tmp_path: Path):
    """Exploder should produce a source window whether tree-sitter is wired or not."""
    src = tmp_path / "thing.py"
    src.write_text(
        dedent(
            """\
            class Trainer:
                def step(self, batch):
                    x = batch["input"]
                    y = self.model(x)
                    return y
            """
        )
    )
    ex = SourceExploder(context_lines=3)
    frame = next(
        iter(parse_trace_frames(f'  File "{src}", line 4, in step\n    y = self.model(x)\n'))
    )
    exploded = ex.explode_frame(frame)
    assert exploded.source is not None
    assert "y = self.model(x)" in exploded.source
    assert ">>>" in exploded.source  # the hot-line marker


def test_explode_skips_library(tmp_path: Path):
    ex = SourceExploder(skip_paths=("/site-packages/",))
    frames = parse_trace_frames(
        '  File "/usr/lib/python3.11/site-packages/torch/nn.py", line 5, in forward\n'
    )
    out = ex.explode_frame(frames[0])
    assert out.skipped is not None
    assert "library" in out.skipped or "vendored" in out.skipped
