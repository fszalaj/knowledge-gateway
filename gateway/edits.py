from __future__ import annotations

import re
from io import StringIO

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

# Round-trip YAML: preserves comments, key order and quote style, and does NOT
# normalise scalars (a frontmatter `published: yes` is not rewritten to `true`).
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096

# A frontmatter fence is a line that is exactly `---` (+ optional trailing
# spaces), followed by a newline or EOF. Matching whole fence LINES (^...$ via
# MULTILINE) means a `---` inside a scalar value (e.g. `title: a---b`) is never
# mistaken for the closing fence. Tolerates CRLF, an empty body and an EOF fence.
_FENCE = re.compile(r"^---[ \t]*(?:\r?\n|\Z)", re.MULTILINE)


def _parse(text: str) -> tuple[str, str, str] | None:
    """If text opens with a frontmatter block, return (block_verbatim, yaml_body,
    body_after); else None."""
    opening = _FENCE.match(text)
    if not opening:
        return None
    closing = _FENCE.search(text, opening.end())
    if not closing:
        return None
    return text[: closing.end()], text[opening.end(): closing.start()], text[closing.end():]


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block_verbatim, body). Block is '' when absent."""
    p = _parse(text)
    return (p[0], p[2]) if p else ("", text)


def read_frontmatter(text: str) -> dict:
    """Parse frontmatter into a mapping. Lenient: returns {} when absent, empty,
    unparseable, or not a mapping - so a single bad note never breaks a query."""
    p = _parse(text)
    if not p:
        return {}
    try:
        data = _yaml.load(p[1])
    except YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def update_frontmatter(text: str, updates: dict) -> str:
    """Apply key updates to the note's frontmatter, preserving the body, comments,
    key order and quote style. Creates a block if the note has none. Raises rather
    than silently discarding metadata when an existing block is unparseable."""
    p = _parse(text)
    if p:
        try:
            data = _yaml.load(p[1])
        except YAMLError as e:
            raise ValueError(f"frontmatter_unparseable: {e}") from e
        if data is None:
            data = {}
        elif not isinstance(data, dict):
            raise ValueError("frontmatter_not_a_mapping")
        body = p[2]
    else:
        data = {}
        body = text
    for k, v in updates.items():
        data[k] = v
    buf = StringIO()
    _yaml.dump(data, buf)
    return f"---\n{buf.getvalue()}---\n{body}"


def insert_markdown(
    text: str,
    content: str,
    *,
    under_heading: str | None = None,
    position: str = "bottom",
) -> str:
    """Insert content into a note without rewriting it.

    - under_heading: insert immediately after the line matching that heading.
    - position 'top': after the frontmatter, before the rest of the body.
    - position 'bottom' (default): at the end.
    Frontmatter is always preserved at the top. Body text is never dropped (only
    surrounding blank lines around the insertion point are normalised).
    """
    fm, body = split_frontmatter(text)
    block = content if content.endswith("\n") else content + "\n"

    if under_heading is not None:
        want = under_heading.strip()
        lines = body.split("\n")
        for i, ln in enumerate(lines):
            if ln.strip() == want and ln.lstrip().startswith("#"):
                head = "\n".join(lines[: i + 1])
                rest = "\n".join(lines[i + 1:]).lstrip("\n")
                tail = f"\n{rest}" if rest else ""
                return f"{fm}{head}\n\n{block}{tail}"
        raise ValueError(f"heading_not_found: {under_heading}")

    if position == "top":
        rest = body.lstrip("\n")
        tail = f"\n{rest}" if rest else ""
        return f"{fm}{block}{tail}"

    if position != "bottom":
        raise ValueError(f"bad_position: {position}")
    trimmed = body.rstrip("\n")
    sep = "\n\n" if trimmed else ""
    return f"{fm}{trimmed}{sep}{block}"


def rewrite_wikilinks(text: str, old_stem: str, new_stem: str) -> tuple[str, int]:
    """Rewrite `[[old]]`, `[[old|alias]]`, `[[old#heading]]`, `[[old^block]]` and
    `![[old]]` to use new_stem, preserving alias/heading/block and the embed `!`.
    Matches the flat note name only - `[[oldfoo]]` is left alone. Returns
    (new_text, links_rewritten)."""
    pat = re.compile(r"(!?\[\[)" + re.escape(old_stem) + r"(?=[\]|#^])")
    count = 0

    def repl(m: "re.Match[str]") -> str:
        nonlocal count
        count += 1
        return m.group(1) + new_stem

    return pat.sub(repl, text), count


def demo() -> None:
    note = "---\ntype: domain\nupdated: 2026-01-01\n---\n# Log\n\n## old\nx\n"
    assert "# Log\n\n## new\ny\n\n## old" in insert_markdown(note, "## new\ny", under_heading="# Log")
    fm = update_frontmatter(note, {"updated": "2026-06-15", "status": "active"})
    d = read_frontmatter(fm)
    assert d["updated"] == "2026-06-15" and d["status"] == "active" and d["type"] == "domain", dict(d)
    assert fm.endswith("# Log\n\n## old\nx\n"), fm
    # CRLF + EOF fence (no trailing newline)
    crlf = "---\r\ntype: x\r\n---"
    assert read_frontmatter(crlf)["type"] == "x", read_frontmatter(crlf)
    # empty frontmatter block
    assert read_frontmatter("---\n---\nbody\n") == {}
    # comment preserved on round-trip
    commented = "---\ntype: x  # keep me\n---\nbody\n"
    assert "# keep me" in update_frontmatter(commented, {"status": "active"})
    # unparseable existing frontmatter -> raise, do not overwrite
    try:
        update_frontmatter("---\n: : :\nbad\n---\nb\n", {"x": 1})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    print("edits.demo OK")


if __name__ == "__main__":
    demo()
