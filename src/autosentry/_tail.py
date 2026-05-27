"""Shared "tail -F"-style file follower.

Used by the SLURM and Attach supervisors. Both follow a single file
that may not exist yet (SLURM creates it after the job starts; an
attached process may rotate its log out from under us). The only
behavioral difference is whether they want to *replay history* on
first open:

- SLURM wants the historical contents (the job may have been running
  before autosentry attached its tail thread).
- Attach wants to skip history (the watched service may have GB of
  pre-existing log).

The ``seek_to_end`` flag controls that choice.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from autosentry.logger import log


def follow_file(
    path_provider: Callable[[], Path | None],
    *,
    on_line: Callable[[str], None],
    stop_event: threading.Event,
    ready_event: threading.Event | None = None,
    seek_to_end: bool,
    error_tag: str,
    missing_backoff_cap: float = 5.0,
) -> None:
    """Follow a file, calling ``on_line`` for each new line.

    ``path_provider`` is a callable so callers can defer path resolution
    (SLURM, for instance, only knows the path once the job has a job id).
    Returns when ``stop_event`` is set.
    """
    backoff = 0.5
    last_inode: int | None = None
    pos = 0
    while not stop_event.is_set():
        path = path_provider()
        if path is None or not path.exists():
            time.sleep(min(backoff, missing_backoff_cap))
            backoff = min(backoff * 1.5, missing_backoff_cap)
            continue
        backoff = 0.5
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                inode = os.fstat(f.fileno()).st_ino
                if last_inode != inode:
                    last_inode = inode
                    if seek_to_end:
                        f.seek(0, 2)  # SEEK_END
                        pos = f.tell()
                    else:
                        pos = 0
                f.seek(pos)
                if ready_event is not None:
                    ready_event.set()
                while not stop_event.is_set():
                    line = f.readline()
                    if not line:
                        pos = f.tell()
                        try:
                            st = path.stat()
                            if st.st_ino != last_inode:
                                # File rotated; reopen.
                                break
                        except FileNotFoundError:
                            break
                        time.sleep(0.5)
                        continue
                    on_line(line.rstrip("\n"))
                    pos = f.tell()
        except OSError as e:
            log().error(f"{error_tag} log tail error: {e}")
            time.sleep(1)
