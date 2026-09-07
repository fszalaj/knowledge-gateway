"""Read-only graph query layer tests (gateway.graph over a built .graph/<name>.json)."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("networkx")

from gateway import graph as graphmod
from gateway.codegraph import build_graph


def _vault_with_graph(tmp_path):
    src = tmp_path / "src"
    (src / "filter_plugins").mkdir(parents=True)
    (src / "filter_plugins/f.py").write_text(
        "def b(x):\n    return x\nclass FilterModule:\n    def filters(self):\n        return {'b': b}\n",
        encoding="utf-8")
    (src / "roles/web/tasks").mkdir(parents=True)
    (src / "roles/web/tasks/main.yml").write_text(
        "- name: t\n  set_fact:\n    y: \"{{ x | b }}\"\n", encoding="utf-8")
    data = build_graph(src)
    gd = tmp_path / graphmod.GRAPH_DIRNAME
    gd.mkdir()
    (gd / "default.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


def test_list_and_stats(tmp_path):
    v = _vault_with_graph(tmp_path)
    graphs = graphmod.list_graphs(v)
    assert graphs and graphs[0]["name"] == "default"
    st = graphmod.stats(v, "default")
    assert st["schema_version"] == 1 and st["node_count"] > 0


def test_query_and_godnodes(tmp_path):
    v = _vault_with_graph(tmp_path)
    hits = graphmod.query(v, "default", "b")
    assert any(h["id"] == "filter:b" for h in hits)
    gods = graphmod.god_nodes(v, "default", top_n=5)
    assert gods and "degree" in gods[0]


def test_neighbors_and_path(tmp_path):
    v = _vault_with_graph(tmp_path)
    nb = graphmod.neighbors(v, "default", "filter:b", depth=1)
    assert nb["center"] == "filter:b"
    assert any(e["relation"] in ("calls_filter", "implemented_by") for e in nb["edges"])
    sp = graphmod.shortest_path(v, "default", "filter:b", "pyfunc:filter_plugins/f.py:b")
    assert sp["length"] == 1


def test_missing_graph_raises(tmp_path):
    (tmp_path / graphmod.GRAPH_DIRNAME).mkdir()
    with pytest.raises(FileNotFoundError, match="graph_not_found"):
        graphmod.query(tmp_path, "nope", "x")


def test_invalid_json_and_bad_name(tmp_path):
    gd = tmp_path / graphmod.GRAPH_DIRNAME
    gd.mkdir()
    (gd / "broken.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="graph_invalid"):
        graphmod.query(tmp_path, "broken", "x")
    with pytest.raises(ValueError, match="graph_invalid"):
        graphmod.graph_file(tmp_path, "a/b")  # path separator rejected
    with pytest.raises(ValueError, match="graph_invalid"):
        graphmod.graph_file(tmp_path, "..\\x")  # backslash separator rejected


def test_neighbors_is_deterministic_centre_first_and_dedupes_edges(tmp_path):
    v = _vault_with_graph(tmp_path)
    node = "pyfunc:filter_plugins/f.py:b"
    runs = [graphmod.neighbors(v, "default", node, depth=2) for _ in range(3)]
    ids = [[n["id"] for n in r["nodes"]] for r in runs]
    assert ids[0] == ids[1] == ids[2]                    # stable across calls
    assert ids[0][0] == node                             # the centre is never truncated away
    edges = [(e["source"], e["target"]) for e in runs[0]["edges"]]
    assert len(edges) == len(set(edges))                 # depth 2 reaches an edge from both ends
    small = graphmod.neighbors(v, "default", node, depth=2, limit=1)
    assert [n["id"] for n in small["nodes"]] == [node] and small["truncated"] is True
