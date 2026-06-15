import pytest

from gateway.detect import VaultDetectionError, detect_vault


def test_wiki_with_md(tmp_path):
    (tmp_path / "wiki").mkdir(); (tmp_path / "wiki" / "a.md").write_text("x")
    assert detect_vault(tmp_path) == tmp_path / "wiki"


def test_empty_wiki_falls_through(tmp_path):
    (tmp_path / "wiki").mkdir()                      # a non-vault "wiki" (e.g. docs)
    (tmp_path / "proj-obsidian-vault").mkdir()
    assert detect_vault(tmp_path) == tmp_path / "proj-obsidian-vault"


def test_single_obsidian_vault(tmp_path):
    (tmp_path / "proj-obsidian-vault").mkdir()
    assert detect_vault(tmp_path) == tmp_path / "proj-obsidian-vault"


def test_ambiguous_obsidian_vaults(tmp_path):
    (tmp_path / "a-obsidian-vault").mkdir(); (tmp_path / "b-obsidian-vault").mkdir()
    with pytest.raises(VaultDetectionError):
        detect_vault(tmp_path)


def test_cwd_is_vault(tmp_path):
    (tmp_path / ".obsidian").mkdir()
    assert detect_vault(tmp_path) == tmp_path


def test_single_child_with_obsidian(tmp_path):
    (tmp_path / "notes").mkdir(); (tmp_path / "notes" / ".obsidian").mkdir()
    assert detect_vault(tmp_path) == tmp_path / "notes"


def test_cwd_top_level_md(tmp_path):
    (tmp_path / "n.md").write_text("x")
    assert detect_vault(tmp_path) == tmp_path


def test_nothing_found(tmp_path):
    (tmp_path / "src").mkdir()
    with pytest.raises(VaultDetectionError):
        detect_vault(tmp_path)
