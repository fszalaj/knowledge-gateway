"""Local stdio mode: what build_local_server actually detects.

The layout detection is asserted directly, because it is invisible through the built server:
`git -C <vault>` finds the same repository whether or not repo_root was widened to the git
root, so a commit lands identically either way. The end-to-end tests below then assert what a
client actually observes - a commit scoped to the vault subdir. Asserting that the server
object exists, as these tests used to, proves nothing about either.
"""
import subprocess

from fastmcp import Client

from gateway.server import build_local_server, repo_layout


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout


def _seed(repo):
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "Tester")


async def test_vault_in_a_larger_repo_commits_only_the_vault_subdir(tmp_path):
    _seed(tmp_path)
    vault = tmp_path / "wiki"
    vault.mkdir()
    (vault / "index.md").write_text("# Index\n")
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "app.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "seed")
    (tmp_path / "code" / "app.py").write_text("x = 2\n")   # a dirty sibling tree

    async with Client(build_local_server(str(vault))) as c:
        r = await c.call_tool("write_note", {"vault": "wiki", "path": "index.md",
                                             "content": "# Index 2\n", "commit": True})
    assert r.data["commit"]["committed"] is True
    changed = _git(tmp_path, "show", "--name-only", "--format=", "HEAD").split()
    assert changed == ["wiki/index.md"]                    # repo_root is the parent, subdir is wiki/
    assert "code/app.py" in _git(tmp_path, "status", "--porcelain")   # sibling not swept in


async def test_vault_that_is_its_own_repo_commits_at_the_root(tmp_path):
    _seed(tmp_path)
    (tmp_path / "note.md").write_text("# n\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "seed")

    async with Client(build_local_server(str(tmp_path))) as c:
        await c.call_tool("write_note", {"vault": tmp_path.name, "path": "note.md",
                                         "content": "# n2\n", "commit": True})
    assert _git(tmp_path, "show", "--name-only", "--format=", "HEAD").split() == ["note.md"]


async def test_non_git_directory_still_serves_its_notes(tmp_path):
    (tmp_path / "note.md").write_text("# n\n")
    async with Client(build_local_server(str(tmp_path))) as c:
        assert (await c.call_tool("list_notes", {"vault": tmp_path.name})).data == ["note.md"]
        assert "# n" in (await c.call_tool("read_note", {"vault": tmp_path.name, "path": "note.md"})).data


def test_repo_layout_detects_a_vault_inside_a_larger_repo(tmp_path):
    _seed(tmp_path)
    vault = tmp_path / "wiki"
    vault.mkdir()
    assert repo_layout(vault) == (tmp_path.resolve(), "wiki")


def test_repo_layout_of_a_vault_that_is_its_own_repo(tmp_path):
    _seed(tmp_path)
    assert repo_layout(tmp_path) == (tmp_path.resolve(), ".")


def test_repo_layout_outside_git_falls_back_to_the_vault(tmp_path):
    vault = tmp_path / "plain"
    vault.mkdir()
    assert repo_layout(vault) == (vault.resolve(), ".")
