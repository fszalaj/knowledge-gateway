from __future__ import annotations

import re
from pathlib import Path

from .search import ripgrep


def _stem(note: str) -> str:
    name = note.rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") else name


def backlinks(root: Path, note: str, *, limit: int = 200) -> list[dict]:
    """Notes that link to `note` via [[wikilink]], [[note|alias]], [[note#h]] or ![[note]]."""
    stem = re.escape(_stem(note))
    pattern = rf"\[\[{stem}(\]|\||#)"
    hits = ripgrep(root, pattern, regex=True, limit=limit)
    target = _stem(note)
    return [h for h in hits if _stem(h["file"]) != target]
