from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .search import ripgrep

# Rust regex has no look-behind, so capture the preceding boundary instead.
_TAG = re.compile(r"(?:^|[^\w/])#([A-Za-z][\w/-]*)")


def list_tags(root: Path, *, limit: int = 5000) -> list[dict]:
    """Inline #tags with occurrence counts. Frontmatter `tags:` is a follow-up."""
    hits = ripgrep(root, r"(^|[^\w/])#[A-Za-z][\w/-]*", regex=True, limit=limit)
    counter: Counter[str] = Counter()
    for h in hits:
        for m in _TAG.finditer(h["text"]):
            counter[m.group(1)] += 1
    return [{"tag": tag, "count": count} for tag, count in counter.most_common()]
