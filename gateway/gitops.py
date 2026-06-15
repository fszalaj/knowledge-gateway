from __future__ import annotations

import os
import subprocess

from .vaults import Vault


def _git(repo_root, *args, env=None, timeout: int = 30) -> str:
    # GIT_LITERAL_PATHSPECS: treat every pathspec as a literal path, so a note whose name
    # begins with pathspec magic (e.g. ':') can never broaden or alter a scoped add/commit.
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**(env or os.environ), "GIT_LITERAL_PATHSPECS": "1"},
    )
    if proc.returncode != 0:
        msg = (proc.stderr.strip() or f"git {' '.join(args)} failed").replace(str(repo_root), "<repo>")
        raise RuntimeError(msg)
    return proc.stdout


def _tracked(repo_root, path: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--error-unmatch", "--", path],
        capture_output=True,
        env={**os.environ, "GIT_LITERAL_PATHSPECS": "1"},
    )
    return proc.returncode == 0


def status(vault: Vault) -> dict:
    out = _git(vault.repo_root, "status", "--porcelain", "--", vault.subdir)
    changes = [line for line in out.splitlines() if line.strip()]
    return {"vault": vault.name, "dirty": bool(changes), "changes": changes}


def commit(vault: Vault, message: str, author: tuple[str, str] | None = None,
           paths: list[str] | None = None) -> dict:
    if not message.strip():
        raise ValueError("bad_message: empty commit message")
    if not message.startswith("wiki:"):
        message = f"wiki: {message}"

    # Scope to the exact paths this op touched when given, else the whole vault subdir.
    # Path-scoping stops a commit=True op from sweeping a concurrent op's still-
    # uncommitted change into (and mis-attributing) this commit.
    if paths:
        # Drop pathspecs that neither exist nor are tracked (e.g. deleting/renaming an
        # untracked note): `git add -- <missing-untracked>` would error.
        pathspec = [p for p in paths if (vault.repo_root / p).exists() or _tracked(vault.repo_root, p)]
    else:
        pathspec = [vault.subdir]
    if not pathspec:
        return {"vault": vault.name, "committed": False, "reason": "nothing to commit"}
    _git(vault.repo_root, "add", "--", *pathspec)
    staged = _git(vault.repo_root, "diff", "--cached", "--name-only", "--", *pathspec)
    if not staged.strip():
        return {"vault": vault.name, "committed": False, "reason": "nothing to commit"}

    # Pathspec-scoped commit: only changes under the vault subdir land in this commit,
    # even if unrelated paths happen to be staged in the same repo. The author is the
    # requesting user (the token's sub); the committer stays the service identity.
    # Per-user environment: both AUTHOR and COMMITTER are the requesting user, so
    # every commit is fully attributed to whoever made it — the OS account the
    # service runs as never appears in git history.
    env = None
    if author and author[0]:
        name = author[0]
        email = author[1] or f"{name}@local"
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
    _git(vault.repo_root, "commit", "-m", message, "--", *pathspec, env=env)
    sha = _git(vault.repo_root, "rev-parse", "HEAD").strip()
    result = {"vault": vault.name, "committed": True, "sha": sha, "message": message}
    if author and author[0]:
        result["author"] = author[0]
    return result
