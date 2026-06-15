import pytest

from gateway.vaults import Vault


def make_vault(tmp_path) -> Vault:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "note.md").write_text("# hi\n[[Other]]\n")
    (tmp_path / "Other.md").write_text("x\n")
    obs = tmp_path / ".obsidian"
    obs.mkdir()
    (obs / "ignored.md").write_text("should be skipped\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n")
    (tmp_path / ".raw").mkdir()
    (tmp_path / ".raw" / "dump.md").write_text("secret source dump\n")
    return Vault(
        name="t",
        path=tmp_path.resolve(),
        repo_root=tmp_path.resolve(),
        subdir=".",
    )


def test_safe_join_blocks_traversal(tmp_path):
    v = make_vault(tmp_path)
    with pytest.raises(PermissionError):
        v.safe_join("../escape.md")
    with pytest.raises(PermissionError):
        v.safe_join("a/../../escape.md")


def test_safe_join_allows_inside(tmp_path):
    v = make_vault(tmp_path)
    assert v.safe_join("a/note.md").is_file()


def test_vault_resolves_symlinked_root(tmp_path):
    # A vault constructed from a symlinked path must still contain correctly: __post_init__
    # resolves the root so safe_join's resolve()'d target stays relative_to it (else every
    # op would raise on macOS /tmp and the like).
    real = tmp_path / "real"
    real.mkdir()
    (real / "note.md").write_text("# hi\n")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    v = Vault(name="t", path=link, repo_root=link, subdir=".")
    assert v.path == real.resolve()
    assert v.safe_join("note.md").is_file()
    assert v.safe_note_path("note.md").read_text() == "# hi\n"


def test_list_markdown_excludes_obsidian(tmp_path):
    v = make_vault(tmp_path)
    notes = v.list_markdown()
    assert "a/note.md" in notes
    assert "Other.md" in notes
    assert all(".obsidian" not in n for n in notes)


def test_list_markdown_respects_limit(tmp_path):
    v = make_vault(tmp_path)
    assert len(v.list_markdown(limit=1)) == 1


def test_safe_join_rejects_excluded_dirs(tmp_path):
    v = make_vault(tmp_path)
    for bad in (".git/config", ".obsidian/ignored.md", ".raw/dump.md"):
        with pytest.raises(PermissionError):
            v.safe_join(bad)


def test_safe_join_rejects_vault_root(tmp_path):
    v = make_vault(tmp_path)
    for bad in ("", ".", "a/.."):
        with pytest.raises(PermissionError):
            v.safe_join(bad)


def test_list_markdown_dot_subdir_is_root(tmp_path):
    v = make_vault(tmp_path)
    assert v.list_markdown(subdir=".") == v.list_markdown()


def test_from_spec_reads_description(tmp_path):
    v = Vault.from_spec(
        "x", {"path": str(tmp_path), "repo_root": str(tmp_path), "subdir": ".", "description": "hi"}
    )
    assert v.description == "hi"


def test_safe_note_path_requires_md(tmp_path):
    v = make_vault(tmp_path)
    (tmp_path / "data.txt").write_text("x\n")
    with pytest.raises(PermissionError):
        v.safe_note_path("data.txt")
    assert v.safe_note_path("a/note.md").name == "note.md"


def test_safe_note_path_rejects_hidden_and_secrets(tmp_path):
    v = make_vault(tmp_path)
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / ".secret.md").write_text("x\n")
    for bad in (".env", ".secret.md", ".git/config"):
        with pytest.raises(PermissionError):
            v.safe_note_path(bad)


def test_list_markdown_skips_hidden_notes(tmp_path):
    v = make_vault(tmp_path)
    (tmp_path / ".secret.md").write_text("x\n")
    assert all(not n.split("/")[-1].startswith(".") for n in v.list_markdown())
