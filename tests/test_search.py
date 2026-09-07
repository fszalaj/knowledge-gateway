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


def test_search_survives_a_non_utf8_note(git_vault):
    # rg reports a non-UTF-8 line as base64 "bytes" instead of "text".
    (git_vault / "Latin.md").write_bytes(b"caf\xe9 find-me-latin\n")
    hits = ripgrep(git_vault, "find-me-latin", regex=False, limit=10)
    assert [h["file"] for h in hits] == ["Latin.md"]
    assert "find-me-latin" in hits[0]["text"]


def test_list_tags_counts_past_the_search_ceiling(git_vault):
    # ripgrep's client-facing 1000-match ceiling must not silently undercount an aggregate.
    (git_vault / "Many.md").write_text("#bulk-tag line\n" * 1200)
    counts = {t["tag"]: t["count"] for t in tags.list_tags(git_vault)}
    assert counts["bulk-tag"] == 1200


def test_rg_text_never_names_a_file_it_cannot_name_exactly():
    from gateway.search import _rg_text
    import base64
    utf8 = {"text": "notes/a.md"}
    latin = {"bytes": base64.b64encode(b"notes/caf\xe9.md").decode()}
    assert _rg_text(utf8, exact=True) == "notes/a.md"
    assert _rg_text(latin, exact=True) is None          # a path must round-trip exactly
    assert "�" in _rg_text(latin)                  # display text may be lossy
