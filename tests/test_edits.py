import pytest

from gateway import edits

NOTE = "---\ntype: domain\nupdated: 2026-01-01\n---\n# Log\n\n## old\nx\n"


def test_insert_under_heading():
    out = edits.insert_markdown(NOTE, "## new\ny", under_heading="# Log")
    assert "# Log\n\n## new\ny\n\n## old\nx\n" in out
    assert out.startswith("---\ntype: domain")


def test_insert_under_heading_missing_raises():
    with pytest.raises(ValueError):
        edits.insert_markdown(NOTE, "x", under_heading="# Nope")


def test_insert_top_after_frontmatter():
    out = edits.insert_markdown(NOTE, "INTRO", position="top")
    body = out.split("---\n", 2)[2]
    assert body.startswith("INTRO\n")
    assert "# Log" in body


def test_insert_bottom():
    assert edits.insert_markdown("a\n", "b", position="bottom") == "a\n\nb\n"
    assert edits.insert_markdown("", "b", position="bottom") == "b\n"


def test_insert_bad_position():
    with pytest.raises(ValueError):
        edits.insert_markdown("a\n", "b", position="sideways")


def test_update_frontmatter_existing():
    out = edits.update_frontmatter(NOTE, {"updated": "2026-06-15", "status": "active"})
    d = edits.read_frontmatter(out)
    assert d == {"type": "domain", "updated": "2026-06-15", "status": "active"}
    assert out.endswith("# Log\n\n## old\nx\n")


def test_update_frontmatter_creates_block_when_absent():
    out = edits.update_frontmatter("# Bare\n", {"type": "concept"})
    assert edits.read_frontmatter(out) == {"type": "concept"}
    assert out.endswith("# Bare\n")


def test_read_frontmatter_absent():
    assert edits.read_frontmatter("# no fm\n") == {}


def test_heading_match_requires_hash():
    note = "---\nt: x\n---\nLog\n\nbody\n"
    with pytest.raises(ValueError):
        edits.insert_markdown(note, "z", under_heading="Log")


def test_frontmatter_crlf_and_eof_fence():
    assert edits.read_frontmatter("---\r\ntype: x\r\n---")["type"] == "x"
    assert edits.read_frontmatter("---\ntype: y\n---")["type"] == "y"


def test_frontmatter_empty_block():
    assert edits.read_frontmatter("---\n---\nbody\n") == {}
    out = edits.update_frontmatter("---\n---\nbody\n", {"type": "x"})
    assert edits.read_frontmatter(out)["type"] == "x"
    assert out.endswith("body\n")


def test_update_frontmatter_preserves_comments():
    out = edits.update_frontmatter("---\ntype: x  # keep\n---\nb\n", {"status": "active"})
    assert "# keep" in out
    assert edits.read_frontmatter(out)["status"] == "active"


def test_update_frontmatter_does_not_normalise_scalars():
    out = edits.update_frontmatter("---\npublished: yes\n---\nb\n", {"status": "active"})
    assert "published: yes" in out


def test_update_frontmatter_raises_on_unparseable():
    with pytest.raises(ValueError):
        edits.update_frontmatter("---\n: : :\nbad\n---\nb\n", {"x": 1})


def test_read_frontmatter_unparseable_is_lenient():
    assert edits.read_frontmatter("---\n: : :\nbad\n---\nb\n") == {}


def test_rewrite_wikilinks():
    t = "[[Old]] x [[Old|a]] y [[Old#h]] z ![[Old]] w [[OldThing]] v [[Other]]"
    out, n = edits.rewrite_wikilinks(t, "Old", "New")
    assert n == 4
    assert out == "[[New]] x [[New|a]] y [[New#h]] z ![[New]] w [[OldThing]] v [[Other]]"


def test_rewrite_wikilinks_block_ref():
    out, n = edits.rewrite_wikilinks("[[Old^b1]] and [[Old^b2|a]] and ![[Old^b3]]", "Old", "New")
    assert n == 3
    assert out == "[[New^b1]] and [[New^b2|a]] and ![[New^b3]]"


def test_rewrite_wikilinks_escapes_regex_and_new_stem():
    # special chars in old_stem matched literally ('.' is not a wildcard), and the
    # new stem is inserted verbatim (no regex backref surprise from a '$1').
    out, n = edits.rewrite_wikilinks("[[A.B (n)]] and [[AxB (n)]]", "A.B (n)", "C$1")
    assert n == 1
    assert out == "[[C$1]] and [[AxB (n)]]"


def test_closing_fence_must_be_own_line():
    note = "---\ntitle: abc---\nstatus: old\n---\nbody\n"
    assert dict(edits.read_frontmatter(note)) == {"title": "abc---", "status": "old"}
    out = edits.update_frontmatter(note, {"x": 1})
    assert dict(edits.read_frontmatter(out)) == {"title": "abc---", "status": "old", "x": 1}
    assert out.endswith("body\n")


def test_rewrite_wikilinks_md_caseinsensitive_and_boundaries():
    t = "[[Beta]] [[beta]] [[Beta.md]] [[BETA#h]] [[Beta^b]] ![[beta]] [[Betafoo]] [[A Beta]]"
    out, n = edits.rewrite_wikilinks(t, "Beta", "Gamma")
    assert n == 6  # Beta, beta, Beta.md, BETA#h, Beta^b, ![[beta]]
    for s in ("[[Gamma]]", "[[Gamma.md]]", "[[Gamma#h]]", "[[Gamma^b]]", "![[Gamma]]"):
        assert s in out, (s, out)
    assert "[[Betafoo]]" in out and "[[A Beta]]" in out  # boundaries: left untouched
