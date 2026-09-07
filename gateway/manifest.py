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

from .writes import atomic_write

SUFFIX = ".meta.yaml"
SCHEMA_VERSION = 1


def path_for(snapshot: Path) -> Path:
    # with_name, not with_suffix: a graph legitimately named `my.graph` would otherwise get
    # `my.meta.yaml` and collide with a graph named `my`.
    return snapshot.with_name(snapshot.stem + SUFFIX)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(source: Path, *args: str, timeout: int = 30) -> str | None:
    """Probe a source tree. None when git is absent, failed, or had to be stopped."""
    try:
        proc = subprocess.Popen(["git", "-C", str(source), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError:  # git is not installed
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # SIGTERM first: this runs against the user's own repository, and the SIGKILL a
        # plain timeout sends would leave .git/index.lock behind - the same bug this
        # package fixed in gitops._git.
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        return None
    return out.strip() if proc.returncode == 0 else None


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
    atomic_write(out, yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    return out


def read(snapshot: Path, contain_to: Path | None = None) -> dict:
    """Provenance for `snapshot`, always with a `status`: `ok`, `missing` or `unreadable`.

    `ok` also carries `snapshot_matches`: False means the snapshot changed after the manifest
    was written, which is exactly the case a caller must not mistake for a fresh graph.

    `contain_to` is the root the sidecar must resolve inside. The snapshot is contained by the
    caller, but its sidecar is a sibling path that could be a symlink pointing out of the
    vault, and what it holds is returned to clients.
    """
    p = path_for(snapshot)
    if not p.is_file():
        return {"status": "missing"}
    if contain_to is not None and not p.resolve().is_relative_to(Path(contain_to).resolve()):
        return {"status": "unreadable"}
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
    # These cross the tool boundary, and a hand-written manifest may hold anything: YAML
    # parses an unquoted timestamp as a datetime and `builder:` as a mapping. Fields whose
    # type callers rely on are normalised to text rather than passed through as they land.
    def _text(value):
        return None if value is None else (value if isinstance(value, str) else str(value))

    return {
        "status": "ok",
        "built_at": _text(doc.get("built_at")),
        "builder": _text(doc.get("builder")),
        "source_root": _text(source.get("root")),
        "revision": _text(source.get("revision")),
        "dirty": source.get("dirty") if isinstance(source.get("dirty"), bool) else None,
        "snapshot_matches": matches,
    }
