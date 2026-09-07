from __future__ import annotations

import base64
import json
import subprocess
import time
from pathlib import Path

from .vaults import EXCLUDE_DIRS

# Derive from the single exclusion source so search can never surface a directory
# that list_notes hides (e.g. .raw/ source dumps, .obsidian-git-data/).
EXCLUDE_GLOBS = [f"!{d}/**" for d in sorted(EXCLUDE_DIRS)]


def _rg_text(field: dict, *, exact: bool = False) -> str | None:
    """rg reports UTF-8 as {"text": ...} and anything else as base64 {"bytes": ...}; one
    latin-1 note must not make search, backlinks and list_tags fail with a KeyError.

    A filename has to name the file back exactly, so `exact` callers get None rather than
    a lossy replacement path that could point at a different note. Line text is display
    only, so there the replacement character is the better answer."""
    if "text" in field:
        return field["text"]
    raw = base64.b64decode(field.get("bytes", ""))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None if exact else raw.decode("utf-8", "replace")


def ripgrep(
    root: Path,
    pattern: str,
    *,
    regex: bool = False,
    limit: int = 50,
    max_limit: int = 1000,
    context: int = 0,
    timeout: int = 20,
    ignore_case: bool = False,
) -> list[dict]:
    limit = max(1, min(limit, max_limit))
    # -i forces case-insensitive (used for wikilink/tag matching, which Obsidian
    # treats case-insensitively); -S (smart case) stays the default for free-text search.
    cmd = ["rg", "--json", "-i" if ignore_case else "-S"]
    if not regex:
        cmd.append("--fixed-strings")
    if context:
        cmd += ["-C", str(context)]
    for g in EXCLUDE_GLOBS:
        cmd += ["--glob", g]
    # Notes only — never surface non-markdown files; -m bounds per-file output.
    cmd += ["--glob", "*.md", "-m", str(limit)]
    cmd += ["--", pattern, str(root)]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ripgrep (rg) is not installed") from exc

    # Stream rg's output and stop as soon as we have `limit` matches, terminating
    # rg so a broad query can't keep scanning (and buffering) the whole vault.
    results: list[dict] = []
    deadline = time.monotonic() + timeout
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            if time.monotonic() > deadline:
                break
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") != "match":
                continue
            data = evt["data"]
            path_text = _rg_text(data["path"], exact=True)
            if path_text is None:  # a filename we cannot name back exactly
                continue
            abs_path = Path(path_text).resolve()
            try:
                rel = abs_path.relative_to(root).as_posix()
            except ValueError:
                rel = path_text
            results.append(
                {
                    "file": rel,
                    "line": data["line_number"],
                    "text": _rg_text(data["lines"]).rstrip("\n"),  # display: never None
                }
            )
            if len(results) >= limit:
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        stderr = proc.stderr.read() if proc.stderr else ""
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()

    # Surface an error only when rg failed outright AND produced nothing. An
    # early terminate (we already had enough) exits via signal (negative rc),
    # which is expected, not a failure. rc 1 = "no matches", also fine.
    rc = proc.returncode
    if not results and rc is not None and rc > 1:
        msg = (stderr.strip() or f"rg failed ({rc})").replace(str(root), "<vault>")
        raise RuntimeError(msg)
    return results
