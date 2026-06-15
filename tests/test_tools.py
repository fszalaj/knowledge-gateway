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
