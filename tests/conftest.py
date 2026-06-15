import subprocess

import pytest

from gateway.server import build_local_server


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def git_vault(tmp_path):
    """A git repo whose root IS the vault, seeded with notes, a frontmatter scalar tag,
    cross links, and a .raw/ file that must never be surfaced."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "Tester")
    (tmp_path / "Alpha.md").write_text(
        "---\ntype: note\ntags: [x]\n---\n# Alpha\nrefs [[Beta]], [[beta#h]] and ![[Beta]]\n"
    )
    (tmp_path / "Beta.md").write_text("---\ntype: note\ntags: y\n---\n# Beta\n#inline-tag here\n")
    (tmp_path / ".raw").mkdir()
    (tmp_path / ".raw" / "dump.md").write_text("SHHH find-me-raw\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "seed")
    return tmp_path


@pytest.fixture
def server(git_vault):
    return build_local_server(str(git_vault))
