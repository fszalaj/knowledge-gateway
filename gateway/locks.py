from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import warnings
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]

_WARNED: set[str] = set()


def _warn_once(msg: str) -> None:
    if msg not in _WARNED:
        warnings.warn(msg, stacklevel=3)
        _WARNED.add(msg)


def _lockfile(repo_root: Path) -> Path:
    # Prefer inside a real .git dir (per-repo, never synced, never a note). Only when .git
    # already exists as a directory - never CREATE it (a non-git vault must not get a
    # spurious .git; a worktree/submodule .git is a file, not a dir). Else a temp file
    # keyed by the repo path.
    git_dir = repo_root / ".git"
    if git_dir.is_dir():
        d = git_dir / "obsidian-gateway-locks"
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d / "write.lock"
        except OSError:
            pass
    h = hashlib.sha256(str(repo_root).encode()).hexdigest()[:16]
    d = Path(tempfile.gettempdir()) / "obsidian-gateway-locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.lock"


@contextlib.contextmanager
def write_lock(repo_root: Path):
    """Serialize writes AND the git commit per repository, across threads and processes
    on this host, so read-modify-write tools and concurrent commits cannot race.

    flock is advisory and NON-reentrant: acquiring it twice for the same repo from one
    thread (two fds) deadlocks - so it is taken once at the tool boundary and the inner
    gitops.commit never re-locks. Degrades to a no-op (one-time warning) where fcntl is
    missing, the lockfile cannot be opened, or the filesystem refuses flock - the write
    proceeds rather than failing.
    """
    if fcntl is None:
        _warn_once("fcntl unavailable; obsidian-gateway write locking is disabled")
        yield
        return
    try:
        fd = os.open(_lockfile(repo_root), os.O_CREAT | os.O_RDWR, 0o666)
    except OSError as e:
        _warn_once(f"cannot open gateway lockfile ({e}); write locking disabled")
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as e:
        _warn_once(f"flock unsupported here ({e}); proceeding without the write lock")
        os.close(fd)
        yield
        return
    try:
        yield
    finally:
        os.close(fd)  # closing releases the flock; no separate LOCK_UN -> no fd leak on error
