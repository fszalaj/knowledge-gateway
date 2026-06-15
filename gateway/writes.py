from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(target: Path, content: str) -> None:
    """Write via a temp file + os.replace so a reader never sees a half-written note
    and a crash mid-write cannot truncate the existing file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
