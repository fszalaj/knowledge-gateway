import os
import subprocess

import pytest

from gateway import gitops
from gateway.vaults import Vault


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


def _show(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True).stdout


def _repo_with_subdir_vault(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "svc@host")
    _git(tmp_path, "config", "user.name", "Service")
    (tmp_path / "wiki").mkdir()
    (tmp_path / "backend").mkdir()
    (tmp_path / "wiki" / "a.md").write_text("# A\n")
    (tmp_path / "backend" / "code.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "seed")
    return Vault(name="wiki", path=tmp_path / "wiki", repo_root=tmp_path, subdir="wiki")


def test_commit_is_subdir_scoped_and_attributed(tmp_path):
    v = _repo_with_subdir_vault(tmp_path)
    # dirty BOTH the vault and a sibling tree
    (tmp_path / "wiki" / "a.md").write_text("# A2\n")
    (tmp_path / "backend" / "code.py").write_text("x = 2\n")

    res = gitops.commit(v, "update a", author=("Alice", "alice@x"))
    assert res["committed"] is True and res["message"].startswith("wiki:")

    changed = _show(tmp_path, "show", "--name-only", "--format=", "HEAD")
    assert "wiki/a.md" in changed
    assert "backend/code.py" not in changed          # sibling NOT swept in
    assert _show(tmp_path, "log", "-1", "--format=%an").strip() == "Alice"  # attributed to requester


def test_status_reflects_subdir(tmp_path):
    v = _repo_with_subdir_vault(tmp_path)
    assert gitops.status(v)["dirty"] is False
    (tmp_path / "wiki" / "a.md").write_text("# A3\n")
    assert gitops.status(v)["dirty"] is True


def test_commit_nothing_to_do(tmp_path):
    v = _repo_with_subdir_vault(tmp_path)
    res = gitops.commit(v, "noop")
    assert res["committed"] is False


def test_git_timeout_asks_git_to_stop_before_killing_it(tmp_path, monkeypatch):
    # SIGKILL would leave .git/index.lock behind and brick every later commit; git only
    # cleans up after itself when it gets a signal it can handle.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "got-sigterm"
    stub = bin_dir / "git"
    # short sleeps, so the trap runs promptly and no grandchild outlives the shell
    stub.write_text(f'#!/bin/sh\ntrap "echo yes > {marker}; exit 143" TERM\n'
                    'i=0\nwhile [ $i -lt 300 ]; do sleep 0.1; i=$((i+1)); done\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    with pytest.raises(RuntimeError, match="timed out"):
        gitops._git(tmp_path, "status", timeout=1)
    assert marker.read_text().strip() == "yes"
