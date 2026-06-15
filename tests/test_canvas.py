import json

import pytest

from gateway.vaults import Vault
from gateway.writes import atomic_write


def _vault(tmp_path):
    return Vault(name="w", path=tmp_path, repo_root=tmp_path, subdir=".")


def test_safe_canvas_path_rejects_non_canvas(tmp_path):
    with pytest.raises(PermissionError, match="not_a_canvas"):
        _vault(tmp_path).safe_canvas_path("note.md")


def test_safe_canvas_path_accepts_canvas(tmp_path):
    v = _vault(tmp_path)
    (tmp_path / "b.canvas").write_text("{}")
    assert v.safe_canvas_path("b.canvas").name == "b.canvas"


def test_list_canvases_excludes_notes_and_system(tmp_path):
    v = _vault(tmp_path)
    (tmp_path / "a.canvas").write_text("{}")
    (tmp_path / "note.md").write_text("x")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "x.canvas").write_text("{}")
    assert v.list_canvases() == ["a.canvas"]


def test_canvas_groups_colors_roundtrip(tmp_path):
    v = _vault(tmp_path)
    p = v.safe_canvas_path("board.canvas")
    canvas = {
        "nodes": [{"id": "g", "type": "group", "label": "Area", "color": "4",
                   "x": 0, "y": 0, "width": 400, "height": 300}],
        "edges": [],
    }
    atomic_write(p, json.dumps(canvas, indent=2) + "\n")
    got = json.loads(p.read_text())
    assert got["nodes"][0]["type"] == "group"
    assert got["nodes"][0]["color"] == "4"
