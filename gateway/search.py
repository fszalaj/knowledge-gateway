from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .vaults import EXCLUDE_DIRS

# Derive from the single exclusion source so search can never surface a directory
# that list_notes hides (e.g. .raw/ source dumps, .obsidian-git-data/).
EXCLUDE_GLOBS = [f"!{d}/**" for d in sorted(EXCLUDE_DIRS)]


def ripgrep(
    root: Path,
    pattern: str,
    *,
    regex: bool = False,
    limit: int = 50,
    context: int = 0,
    timeout: int = 20,
) -> list[dict]:
    limit = max(1, min(limit, 1000))
    cmd = ["rg", "--json", "-S"]
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
            abs_path = Path(data["path"]["text"]).resolve()
            try:
                rel = abs_path.relative_to(root).as_posix()
            except ValueError:
                rel = data["path"]["text"]
            results.append(
                {
                    "file": rel,
                    "line": data["line_number"],
                    "text": data["lines"]["text"].rstrip("\n"),
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
