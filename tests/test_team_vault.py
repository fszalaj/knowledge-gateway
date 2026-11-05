"""Team vault kit: scripts/sync-repos.sh and scripts/roster_check.py, run against local stand-ins (no network)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
GIT = shutil.which("git")


def _git(*args: str, cwd: Path) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run([GIT, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _vault(tmp_path: Path, script: str) -> Path:
    vault = tmp_path / "vault"
    (vault / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / script, vault / "scripts" / script)
    return vault


# ---------------------------------------------------------------------------------------------- sync-repos.sh

@pytest.fixture
def remotes(tmp_path):
    if BASH is None or GIT is None or os.name == "nt":
        pytest.skip("bash and git on a POSIX system are required")
    base = tmp_path / "remotes"
    work = tmp_path / "work"
    for name in ("alpha", "beta"):
        src = work / name
        src.mkdir(parents=True)
        _git("init", "-q", "-b", "main", cwd=src)
        (src / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        _git("add", "-A", cwd=src)
        _git("commit", "-q", "-m", "init", cwd=src)
        _git("branch", "feature/x", cwd=src)
        _git("clone", "-q", "--bare", str(src), str(base / name), cwd=tmp_path)
    return base, work


def _sync(vault: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, "scripts/sync-repos.sh", *args], cwd=vault, capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def test_sync_clones_then_fast_forwards(tmp_path, remotes):
    base, work = remotes
    vault = _vault(tmp_path, "sync-repos.sh")
    (vault / "repos.txt").write_text(f"# git-base: file://{base}/\nalpha main\nbeta main feature/x\n", encoding="utf-8")
    first = _sync(vault)
    assert first.returncode == 0, first.stderr
    assert (vault / "repos" / "alpha" / "README.md").is_file()
    assert "origin/feature/x" in first.stdout
    (work / "alpha" / "NEW.md").write_text("new\n", encoding="utf-8")
    _git("add", "-A", cwd=work / "alpha")
    _git("commit", "-q", "-m", "second", cwd=work / "alpha")
    _git("push", "-q", str(base / "alpha"), "main", cwd=work / "alpha")
    second = _sync(vault)
    assert second.returncode == 0, second.stderr
    assert (vault / "repos" / "alpha" / "NEW.md").is_file()
    assert "up to date" in second.stdout


def test_sync_dry_run_changes_nothing(tmp_path, remotes):
    base, _ = remotes
    vault = _vault(tmp_path, "sync-repos.sh")
    (vault / "repos.txt").write_text(f"# git-base: file://{base}/\nalpha main\n", encoding="utf-8")
    result = _sync(vault, "--dry-run")
    assert result.returncode == 0
    assert "would clone" in result.stdout
    assert not (vault / "repos" / "alpha").exists()


def test_sync_needs_a_clone_base(tmp_path, remotes):
    base, _ = remotes
    vault = _vault(tmp_path, "sync-repos.sh")
    (vault / "repos.txt").write_text("alpha main\n", encoding="utf-8")
    result = _sync(vault, env={"REPOS_GIT_BASE": ""})
    assert result.returncode == 2
    assert "git-base" in result.stderr
    with_env = _sync(vault, "--only", "alpha", env={"REPOS_GIT_BASE": f"file://{base}"})
    assert with_env.returncode == 0, with_env.stderr
    assert (vault / "repos" / "alpha").is_dir()


def test_sync_leaves_local_changes_and_bad_lines_alone(tmp_path, remotes):
    base, _ = remotes
    vault = _vault(tmp_path, "sync-repos.sh")
    (vault / "repos.txt").write_text(f"# git-base: file://{base}/\nalpha main\n", encoding="utf-8")
    assert _sync(vault).returncode == 0
    (vault / "repos" / "alpha" / "README.md").write_text("local edit\n", encoding="utf-8")
    skipped = _sync(vault)
    assert skipped.returncode == 0
    assert "skipped (local changes)" in skipped.stdout
    assert (vault / "repos" / "alpha" / "README.md").read_text(encoding="utf-8") == "local edit\n"
    (vault / "repos.txt").write_text(f"# git-base: file://{base}/\n../evil main\nnobranch\n", encoding="utf-8")
    bad = _sync(vault)
    assert bad.returncode == 1
    assert "invalid repository name" in bad.stderr and "has no default branch" in bad.stderr


# ---------------------------------------------------------------------------------------------- roster_check.py

FAKE_GH = """\
import json, os, sys
args = sys.argv[1:]
if os.environ.get("FAKE_GH_FAIL"):
    sys.stderr.write("HTTP 404: Not Found\\n"); sys.exit(1)
path = next(a for a in args if a.startswith("orgs/"))
data = json.loads(os.environ["FAKE_GH_DATA"])
for item in data["members" if path.endswith("/members") else "repos"]:
    print(json.dumps(item))
"""


@pytest.fixture
def roster(tmp_path):
    vault = _vault(tmp_path, "roster_check.py")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "fake_gh.py").write_text(FAKE_GH, encoding="utf-8")
    if os.name == "nt":
        (bindir / "gh.cmd").write_text(f'@"{sys.executable}" "%~dp0fake_gh.py" %*\n', encoding="utf-8")
    else:
        gh = bindir / "gh"
        gh.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{bindir / 'fake_gh.py'}' \"$@\"\n", encoding="utf-8")
        gh.chmod(0o755)
    (vault / "team.txt").write_text("# github-team: acme/platform\nalice maintainer\nbob member\n", encoding="utf-8")
    (vault / "repos.txt").write_text("# git-base: https://github.com/acme/\nsvc main\nold main\n", encoding="utf-8")

    def run(members, repos, *args, fail=False):
        env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
               "FAKE_GH_DATA": json.dumps({"members": [{"login": m} for m in members], "repos": repos})}
        if fail:
            env["FAKE_GH_FAIL"] = "1"
        return subprocess.run([sys.executable, "scripts/roster_check.py", *args], cwd=vault, env=env,
                              capture_output=True, text=True)
    return vault, run


def test_roster_in_sync(roster):
    _, run = roster
    result = run(["alice", "bob"], [{"name": "svc", "default_branch": "main"}, {"name": "old", "default_branch": "main"}])
    assert result.returncode == 0, result.stderr
    assert "acme/platform" in result.stdout and "in sync" in result.stdout


def test_roster_reports_every_kind_of_difference(roster):
    _, run = roster
    result = run(["alice", "Bob", "carol"],
                 [{"name": "svc", "default_branch": "develop"}, {"name": "new", "default_branch": "main", "archived": True}],
                 "--json")
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    kinds = sorted((d["kind"], d["name"]) for d in report["differences"])
    assert kinds == [("member-added", "carol"), ("member-case", "Bob"), ("repo-added", "new"),
                     ("repo-default-branch", "svc"), ("repo-removed", "old")]
    assert report["org"] == "acme" and report["team"] == "platform" and report["in_sync"] is False


def test_roster_needs_a_team_and_github(roster):
    vault, run = roster
    assert run([], [], fail=True).returncode == 2
    (vault / "team.txt").write_text("alice maintainer\n", encoding="utf-8")
    missing = run(["alice"], [])
    assert missing.returncode == 2 and "github-team" in missing.stderr
    explicit = run(["alice"], [{"name": "svc", "default_branch": "main"}, {"name": "old", "default_branch": "main"}],
                   "--team", "acme/platform")
    assert explicit.returncode == 0, explicit.stderr


def test_example_files_document_the_directives():
    repos = (ROOT / "repos.example.txt").read_text(encoding="utf-8")
    team = (ROOT / "team.example.txt").read_text(encoding="utf-8")
    assert "# git-base:" in repos and "# github-team:" in team
    # Every example line is a comment, so copying the template yields an empty, valid roster.
    assert all(not line.strip() or line.lstrip().startswith("#") for line in (repos + team).splitlines())
