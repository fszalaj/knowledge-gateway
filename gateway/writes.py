from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def atomic_write(target: Path, content: str) -> None:
    """Write via a temp file + os.replace so a reader never sees a half-written note
    and a crash mid-write cannot truncate the existing file. The target keeps its
    existing permissions; a new note gets 0644, not mkstemp's restrictive 0600."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            mode = stat.S_IMODE(os.stat(target).st_mode)  # overwrite: keep perms
        except FileNotFoundError:
            mode = 0o644  # new note: readable, not mkstemp's 0600
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
