from pathlib import Path

import pytest
from fastmcp import Client

from gateway import edits


async def test_write_read_roundtrip(server, git_vault):
    vault = git_vault.name
    async with Client(server) as c:
        await c.call_tool("write_note", {"vault": vault, "path": "New.md", "content": "# New\nhi\n"})
        r = await c.call_tool("read_note", {"vault": vault, "path": "New.md"})
    assert "hi" in r.data


async def test_list_vaults_and_notes_exclude_raw(server, git_vault):
    vault = git_vault.name
    async with Client(server) as c:
        vaults = (await c.call_tool("list_vaults", {})).data
        notes = (await c.call_tool("list_notes", {"vault": vault})).data
    assert vault in [v["vault"] for v in vaults]
    assert "Alpha.md" in notes and "Beta.md" in notes
    assert not any(".raw" in n for n in notes)  # .raw/ never surfaced


async def test_patch_frontmatter_delete(server, git_vault):
    vault = git_vault.name
    async with Client(server) as c:
        await c.call_tool("patch_note", {"vault": vault, "path": "Beta.md", "content": "added-line", "position": "bottom"})
        assert "added-line" in (git_vault / "Beta.md").read_text()
        await c.call_tool("patch_frontmatter", {"vault": vault, "path": "Beta.md", "updates": {"status": "active"}})
        assert edits.read_frontmatter((git_vault / "Beta.md").read_text())["status"] == "active"
        await c.call_tool("delete_note", {"vault": vault, "path": "Beta.md"})
    assert not (git_vault / "Beta.md").exists()


async def test_query_notes_scalar_and_list_tags(server, git_vault):
    vault = git_vault.name
    async with Client(server) as c:
        by_y = (await c.call_tool("query_notes", {"vault": vault, "tag": "y"})).data   # Beta: tags: y (scalar)
        by_x = (await c.call_tool("query_notes", {"vault": vault, "tag": "x"})).data   # Alpha: tags: [x]
    assert any(n["path"] == "Beta.md" for n in by_y)
    assert any(n["path"] == "Alpha.md" for n in by_x)


async def test_search_excludes_raw(server, git_vault):
    async with Client(server) as c:
        r = await c.call_tool("search", {"vault": git_vault.name, "query": "find-me-raw"})
    assert r.data == []  # the match lives in .raw/, which is excluded


async def test_rename_rewrites_links_case_insensitive(server, git_vault):
    vault = git_vault.name
    async with Client(server) as c:
        r = await c.call_tool("rename_note", {"vault": vault, "old_path": "Beta.md", "new_path": "Gamma.md"})
    assert r.data["links_updated"] == 3  # [[Beta]], [[beta#h]] (lowercase), ![[Beta]]
    alpha = (git_vault / "Alpha.md").read_text()
    assert "[[Gamma]]" in alpha and "[[Gamma#h]]" in alpha and "![[Gamma]]" in alpha
    assert "Beta" not in alpha
    assert (git_vault / "Gamma.md").exists() and not (git_vault / "Beta.md").exists()


async def test_rename_refuses_existing_target(server, git_vault):
    async with Client(server) as c:
        with pytest.raises(Exception) as e:
            await c.call_tool("rename_note", {"vault": git_vault.name, "old_path": "Beta.md", "new_path": "Alpha.md"})
    assert "exists" in str(e.value).lower()


async def test_read_not_found(server, git_vault):
    async with Client(server) as c:
        with pytest.raises(Exception) as e:
            await c.call_tool("read_note", {"vault": git_vault.name, "path": "Nope.md"})
    assert "not_found" in str(e.value)


async def test_read_too_large(server, git_vault, monkeypatch):
    import gateway.tools as t
    monkeypatch.setattr(t, "MAX_NOTE_BYTES", 5)
    async with Client(server) as c:
        with pytest.raises(Exception) as e:
            await c.call_tool("read_note", {"vault": git_vault.name, "path": "Alpha.md"})
    assert "too_large" in str(e.value)


async def test_query_notes_skips_escaping_symlink_and_unreadable_notes(server, git_vault):
    outside = git_vault.parent / "outside.md"   # git_vault IS tmp_path, so go one level up
    outside.write_text("---\ntype: secret\n---\n")
    (git_vault / "Link.md").symlink_to(outside)              # escapes the vault
    (git_vault / "Latin.md").write_bytes(b"---\ntype: note\n---\ncaf\xe9\n")  # not UTF-8
    async with Client(server) as c:
        notes = (await c.call_tool("query_notes", {"vault": git_vault.name})).data
    paths = {n["path"] for n in notes}
    assert "Alpha.md" in paths                                # the query still answers
    assert "Link.md" not in paths and "Latin.md" not in paths
    assert not any(n["type"] == "secret" for n in notes)


async def test_rename_with_trailing_slash_does_not_rewrite_bare_links(server, git_vault):
    (git_vault / "Alpha.md").write_text("[[#heading]] and [[|alias]] and [[Beta]]\n")
    async with Client(server) as c:
        r = await c.call_tool("rename_note",
                              {"vault": git_vault.name, "old_path": "Beta.md/", "new_path": "Delta.md"})
    assert r.data["links_updated"] == 1
    assert (git_vault / "Alpha.md").read_text() == "[[#heading]] and [[|alias]] and [[Delta]]\n"


async def test_read_canvas_rejects_non_object_json(server, git_vault):
    (git_vault / "board.canvas").write_text("[1, 2]")
    async with Client(server) as c:
        with pytest.raises(Exception, match="canvas_invalid"):
            await c.call_tool("read_canvas", {"vault": git_vault.name, "path": "board.canvas"})


async def test_convert_refuses_a_non_document_file(server, git_vault):
    (git_vault / "credentials.yaml").write_text("token: shhh\n")
    async with Client(server) as c:
        with pytest.raises(Exception, match="not_convertible"):
            await c.call_tool("convert_to_markdown", {"vault": git_vault.name, "path": "credentials.yaml"})


async def test_rename_survives_a_note_it_cannot_read(server, git_vault):
    (git_vault / "Latin.md").write_bytes(b"caf\xe9 [[Beta]]\n")   # not UTF-8
    async with Client(server) as c:
        r = await c.call_tool("rename_note",
                              {"vault": git_vault.name, "old_path": "Beta.md", "new_path": "Gamma.md"})
    assert r.data["links_updated"] == 3                          # Alpha.md still rewritten
    assert (git_vault / "Gamma.md").exists()


async def test_rename_writes_a_hardlinked_note_at_its_own_path(server, git_vault):
    # A hardlink shares src's inode, so "is this the moved note?" cannot be answered by
    # inode alone: the moved note is the one whose own name is gone after the move.
    (git_vault / "Beta.md").write_text("self [[Beta]]\n")
    (git_vault / "Copy.md").hardlink_to(git_vault / "Beta.md")
    async with Client(server) as c:
        r = await c.call_tool("rename_note",
                              {"vault": git_vault.name, "old_path": "Beta.md", "new_path": "Gamma.md"})
    assert sorted(r.data["files"]) == ["Alpha.md", "Copy.md", "Gamma.md"]
    assert "[[Gamma]]" in (git_vault / "Copy.md").read_text()
    assert "[[Beta]]" not in (git_vault / "Copy.md").read_text()


async def test_query_notes_survives_a_symlink_loop(server, git_vault):
    # Path.resolve() raises RuntimeError (not OSError) for a loop up to Python 3.12.
    (git_vault / "loop_a.md").symlink_to(git_vault / "loop_b.md")
    (git_vault / "loop_b.md").symlink_to(git_vault / "loop_a.md")
    async with Client(server) as c:
        notes = (await c.call_tool("query_notes", {"vault": git_vault.name})).data
    assert "Alpha.md" in {n["path"] for n in notes}


async def test_convert_refuses_a_file_over_the_cap(server, git_vault, monkeypatch):
    # The size check lives past the converter import, so this needs the [convert] extra;
    # without it the honest answer is convert_unavailable, not too_large.
    pytest.importorskip("markitdown")
    from gateway import convert as convertmod
    (git_vault / "big.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 4096)
    monkeypatch.setattr(convertmod, "MAX_CONVERT_BYTES", 1024)
    async with Client(server) as c:
        with pytest.raises(Exception, match="too_large"):
            await c.call_tool("convert_to_markdown", {"vault": git_vault.name, "path": "big.pdf"})


async def test_graph_build_rejects_an_unknown_language_as_a_client_error(server, git_vault, tmp_path):
    # graph_build reaches the builder directly, and its refusal must arrive as a
    # deliberate gateway error rather than a masked internal one.
    pytest.importorskip("networkx")
    src = tmp_path / "src_tree"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n")
    async with Client(server) as c:
        with pytest.raises(Exception, match="graph_invalid: unknown language"):
            await c.call_tool("graph_build", {"vault": git_vault.name, "source": str(src),
                                              "name": "g", "languages": ["js"]})
        r = await c.call_tool("graph_build", {"vault": git_vault.name, "source": str(src),
                                              "name": "g", "languages": ["python"]})
    assert r.data["nodes"] > 0          # naming a native pass is not an error


async def test_rename_survives_a_note_that_vanishes_during_the_scan(server, git_vault, monkeypatch):
    # The identity check runs inside the guarded read, so a note deleted between the read
    # and that check drops out of the rename instead of aborting it.
    (git_vault / "Ghost.md").write_text("[[Beta]]\n")
    real = Path.samefile

    def flaky(self, other):
        if self.name == "Ghost.md":
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real(self, other)

    monkeypatch.setattr(Path, "samefile", flaky)
    async with Client(server) as c:
        r = await c.call_tool("rename_note",
                              {"vault": git_vault.name, "old_path": "Beta.md", "new_path": "Gamma.md"})
    assert "Alpha.md" in r.data["files"] and "Ghost.md" not in r.data["files"]
    assert (git_vault / "Gamma.md").exists()


async def test_rename_accepts_a_case_mismatched_old_path(server, git_vault, tmp_path):
    probe = tmp_path / "CaseProbe"
    probe.write_text("x")
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("case-sensitive filesystem: 'beta.md' is not 'Beta.md' here")
    async with Client(server) as c:
        await c.call_tool("rename_note",
                          {"vault": git_vault.name, "old_path": "beta.md", "new_path": "Gamma.md"})
    assert (git_vault / "Gamma.md").exists()
    assert not (git_vault / "Beta.md").exists()          # not re-created under its old name
    assert "[[Gamma]]" in (git_vault / "Alpha.md").read_text()


async def test_graph_build_writes_provenance_that_graph_stats_reports(server, git_vault, tmp_path):
    pytest.importorskip("networkx")
    src = tmp_path / "tree"
    src.mkdir()
    (src / "a.py").write_text("def a():\n    return 1\n")
    async with Client(server) as c:
        built = await c.call_tool("graph_build", {"vault": git_vault.name, "source": str(src),
                                                  "name": "kg"})
        stats = await c.call_tool("graph_stats", {"vault": git_vault.name, "name": "kg"})
        listed = await c.call_tool("list_graphs", {"vault": git_vault.name})
    assert built.data["provenance"] == "kg.meta.yaml"
    prov = stats.data["provenance"]
    assert prov["status"] == "ok" and prov["snapshot_matches"] is True
    assert prov["source_root"] == "tree" and prov["builder"].startswith("knowledge-gateway ")
    assert [g["provenance"] for g in listed.data if g["name"] == "kg"] == ["ok"]


async def test_rename_counts_a_symlinked_note_once(server, git_vault):
    # safe_note_path resolves a vault-internal symlink, so the link and its target were the
    # same file twice: written twice and counted twice, reporting more rewrites than happened.
    (git_vault / "Link.md").symlink_to(git_vault / "Alpha.md")
    async with Client(server) as c:
        r = await c.call_tool("rename_note",
                              {"vault": git_vault.name, "old_path": "Beta.md", "new_path": "Gamma.md"})
    assert r.data["files"] == ["Alpha.md"]
    assert r.data["links_updated"] == 3            # [[Beta]], [[beta#h]], ![[Beta]] - once each
    assert (git_vault / "Link.md").is_symlink()
    assert "[[Gamma]]" in (git_vault / "Alpha.md").read_text()
