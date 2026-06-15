from gateway import links, tags
from gateway.search import ripgrep


def test_literal_search_and_raw_excluded(git_vault):
    assert ripgrep(git_vault, "find-me-raw", regex=False, limit=10) == []  # .raw/ excluded
    hits = ripgrep(git_vault, "Beta", regex=False, limit=10)
    assert any(h["file"] == "Alpha.md" for h in hits)


def test_regex_search(git_vault):
    hits = ripgrep(git_vault, r"#[a-z-]+-tag", regex=True, limit=10)
    assert any("inline-tag" in h["text"] for h in hits)


def test_backlinks_case_insensitive_and_self_excluded(git_vault):
    files = {h["file"] for h in links.backlinks(git_vault, "Beta")}
    assert "Alpha.md" in files       # finds [[Beta]], [[beta#h]], ![[Beta]]
    assert "Beta.md" not in files    # a note is not its own backlink


def test_list_tags_counts_inline(git_vault):
    assert "inline-tag" in {t["tag"] for t in tags.list_tags(git_vault)}
