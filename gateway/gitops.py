from __future__ import annotations

import os
import subprocess

from .vaults import Vault


def _git(repo_root, *args, env=None, timeout: int = 30) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if proc.returncode != 0:
        msg = (proc.stderr.strip() or f"git {' '.join(args)} failed").replace(str(repo_root), "<repo>")
        raise RuntimeError(msg)
    return proc.stdout


def status(vault: Vault) -> dict:
    out = _git(vault.repo_root, "status", "--porcelain", "--", vault.subdir)
    changes = [line for line in out.splitlines() if line.strip()]
    return {"vault": vault.name, "dirty": bool(changes), "changes": changes}


def commit(vault: Vault, message: str, author: tuple[str, str] | None = None) -> dict:
    if not message.strip():
        raise ValueError("empty commit message")
    if not message.startswith("wiki:"):
        message = f"wiki: {message}"

    _git(vault.repo_root, "add", "--", vault.subdir)
    staged = _git(vault.repo_root, "diff", "--cached", "--name-only", "--", vault.subdir)
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
    _git(vault.repo_root, "commit", "-m", message, "--", vault.subdir, env=env)
    sha = _git(vault.repo_root, "rev-parse", "HEAD").strip()
    result = {"vault": vault.name, "committed": True, "sha": sha, "message": message}
    if author and author[0]:
        result["author"] = author[0]
    return result
