import pytest

from gateway.vaults import IMAGE_FORMATS, Vault


def _vault(tmp_path):
    return Vault(name="w", path=tmp_path, repo_root=tmp_path, subdir=".")


def test_safe_attachment_path_accepts_image_and_pdf(tmp_path):
    v = _vault(tmp_path)
    (tmp_path / "img.png").write_bytes(b"x")
    (tmp_path / "doc.pdf").write_bytes(b"x")
    assert v.safe_attachment_path("img.png").name == "img.png"
    assert v.safe_attachment_path("doc.pdf").name == "doc.pdf"


def test_safe_attachment_path_rejects_md(tmp_path):
    with pytest.raises(PermissionError, match="not_an_attachment"):
        _vault(tmp_path).safe_attachment_path("note.md")


def test_safe_attachment_path_rejects_traversal(tmp_path):
    with pytest.raises(PermissionError, match="path_escape"):
        _vault(tmp_path).safe_attachment_path("../evil.png")


def test_safe_attachment_path_rejects_hidden(tmp_path):
    with pytest.raises(PermissionError, match="path_hidden"):
        _vault(tmp_path).safe_attachment_path(".secret/x.png")


def test_list_attachments_excludes_notes_and_system(tmp_path):
    v = _vault(tmp_path)
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "b.pdf").write_bytes(b"x")
    (tmp_path / "note.md").write_text("x")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "c.png").write_bytes(b"x")
    got = v.list_attachments()
    assert "a.png" in got and "b.pdf" in got
    assert "note.md" not in got
    assert ".obsidian/c.png" not in got


def test_image_formats_normalize_jpg():
    # jpg -> jpeg so the FastMCP Image mime is image/jpeg, not the invalid image/jpg
    assert IMAGE_FORMATS[".jpg"] == "jpeg"
    assert IMAGE_FORMATS[".jpeg"] == "jpeg"


async def test_read_attachment_reports_the_real_media_type(server, git_vault):
    from fastmcp import Client
    (git_vault / "song.mp3").write_bytes(b"ID3\x03\x00\x00\x00")
    (git_vault / "doc.pdf").write_bytes(b"%PDF-1.4\n")
    async with Client(server) as c:
        for name, mime in (("song.mp3", "audio/mpeg"), ("doc.pdf", "application/pdf")):
            r = await c.call_tool("read_attachment", {"vault": git_vault.name, "path": name})
            res = r.content[0].resource
            assert res.mimeType == mime                    # not application/<extension>
            assert str(res.uri) == f"file:///{name}"       # no doubled extension, no server path
