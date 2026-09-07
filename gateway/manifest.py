"""Provenance sidecar for a built code graph: `<vault>/.graph/<name>.meta.yaml`.

A graph is a snapshot. It cannot refuse to be stale, and a stale one answers confidently
about a file that is gone - so the snapshot carries what it was built from, and every query
hands that back with the answer instead of leaving the caller to trust a wiki page.

The sidecar is written next to the snapshot by whatever builds it (the `graph_build` tool and
the `knowledge-gateway-graph` CLI) and read back by `graph_stats` / `list_graphs`. It records
the source revision, the build time and the snapshot's SHA-256; reading verifies that hash, so
a snapshot rebuilt or edited without its manifest reports `snapshot_matches: false` rather than
a plausible-looking revision that is not what the file contains.

Deliberately free of absolute paths: only the source directory's basename is stored, because
these fields are returned to clients, including in shared HTTP mode.
"""
from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime
from pathlib import Path

import yaml

SUFFIX = ".meta.yaml"
SCHEMA_VERSION = 1


def path_for(snapshot: Path) -> Path:
    return snapshot.with_suffix("").with_suffix(SUFFIX) if snapshot.suffix == ".json" \
        else Path(str(snapshot) + SUFFIX)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(source: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(source), *args],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):  # no git, or it hung
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def source_revision(source: Path) -> tuple[str | None, bool | None]:
    """(commit, dirty) for a source tree, or (None, None) when it is not a git checkout."""
    rev = _git(source, "rev-parse", "HEAD")
    if rev is None:
        return None, None
    status = _git(source, "status", "--porcelain")
    return rev, bool(status) if status is not None else None


def write(snapshot: Path, graph_meta: dict, source: Path, builder_version: str,
          now: datetime | None = None) -> Path:
    """Write the sidecar for `snapshot`. Returns its path."""
    rev, dirty = source_revision(source)
    stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    doc = {
        "schema_version": SCHEMA_VERSION,
        "graph_name": snapshot.stem,
        "built_at": stamp,
        "builder": f"knowledge-gateway {builder_version}",
        "source": {"root": Path(source).name, "revision": rev, "dirty": dirty},
        "snapshot": {
            "sha256": _sha256(snapshot),
            "node_count": graph_meta.get("node_count"),
            "edge_count": graph_meta.get("edge_count"),
        },
    }
    out = path_for(snapshot)
    out.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return out


def read(snapshot: Path) -> dict:
    """Provenance for `snapshot`, always with a `status`: `ok`, `missing` or `unreadable`.

    `ok` also carries `snapshot_matches`: False means the snapshot changed after the manifest
    was written, which is exactly the case a caller must not mistake for a fresh graph.
    """
    p = path_for(snapshot)
    if not p.is_file():
        return {"status": "missing"}
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {"status": "unreadable"}
    if not isinstance(doc, dict):
        return {"status": "unreadable"}
    source = doc.get("source") if isinstance(doc.get("source"), dict) else {}
    snap = doc.get("snapshot") if isinstance(doc.get("snapshot"), dict) else {}
    recorded = snap.get("sha256")
    try:
        matches = bool(recorded) and recorded == _sha256(snapshot)
    except OSError:
        matches = False
    return {
        "status": "ok",
        "built_at": doc.get("built_at"),
        "builder": doc.get("builder"),
        "source_root": source.get("root"),
        "revision": source.get("revision"),
        "dirty": source.get("dirty"),
        "snapshot_matches": matches,
    }
