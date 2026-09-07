from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .search import ripgrep

# Rust regex has no look-behind, so capture the preceding boundary instead.
_TAG = re.compile(r"(?:^|[^\w/])#([A-Za-z][\w/-]*)")


def list_tags(root: Path, *, limit: int = 50_000) -> list[dict]:
    """Inline #tags with occurrence counts. Frontmatter `tags:` is a follow-up.

    `limit` is the ceiling on tagged LINES scanned, not on tags returned. It is far above
    any real vault rather than exact: counting every line without a bound would let one
    pathological file hold the whole scan in memory."""
    # max_limit too: counting is an aggregate, so ripgrep's client-facing 1000-match
    # ceiling would silently undercount tags (and drop rare ones) in a large vault.
    hits = ripgrep(root, r"(^|[^\w/])#[A-Za-z][\w/-]*", regex=True, limit=limit,
                   max_limit=limit)
    counter: Counter[str] = Counter()
    for h in hits:
        for m in _TAG.finditer(h["text"]):
            counter[m.group(1)] += 1
    return [{"tag": tag, "count": count} for tag, count in counter.most_common()]
