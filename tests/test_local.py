import subprocess

from gateway.server import build_local_server


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_build_local_server_detects_repo_root_and_subdir(tmp_path):
    # vault is a subdir of a larger git repo
    _git(tmp_path, "init")
    vault = tmp_path / "wiki"
    vault.mkdir()
    (vault / "index.md").write_text("---\ntype: domain\n---\n# Index\n")

    mcp = build_local_server(str(vault))
    assert mcp is not None  # constructs without a token / auth


def test_build_local_server_standalone_repo(tmp_path):
    # vault IS the repo root
    _git(tmp_path, "init")
    (tmp_path / "note.md").write_text("# n\n")
    mcp = build_local_server(str(tmp_path))
    assert mcp is not None


def test_build_local_server_non_git_path(tmp_path):
    # not a git repo at all - still builds (git ops would just no-op later)
    (tmp_path / "note.md").write_text("# n\n")
    mcp = build_local_server(str(tmp_path))
    assert mcp is not None
