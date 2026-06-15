import pytest

from gateway.vaults import Vault


def _vault(tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    return Vault(name="v", path=vault, repo_root=vault, subdir=".")


def test_blocks_parent_traversal(tmp_path):
    v = _vault(tmp_path)
    (tmp_path / "secret.md").write_text("s")
    with pytest.raises(PermissionError):
        v.safe_note_path("../secret.md")


def test_blocks_symlink_escape(tmp_path):
    v = _vault(tmp_path)
    (tmp_path / "secret.md").write_text("s")
    (v.path / "link.md").symlink_to(tmp_path / "secret.md")  # points outside the vault
    with pytest.raises(PermissionError):
        v.safe_note_path("link.md")


def test_blocks_hidden_and_non_md(tmp_path):
    v = _vault(tmp_path)
    with pytest.raises(PermissionError):
        v.safe_note_path(".env")
    with pytest.raises(PermissionError):
        v.safe_note_path("notes/.secret.md")
    with pytest.raises(PermissionError):
        v.safe_note_path("note.txt")


def test_allows_normal_nested_note(tmp_path):
    v = _vault(tmp_path)
    assert v.safe_note_path("sub/dir/note.md") == v.path / "sub" / "dir" / "note.md"
