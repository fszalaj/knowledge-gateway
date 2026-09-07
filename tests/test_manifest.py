"""The provenance sidecar: what a snapshot says about where it came from."""
from __future__ import annotations

import json
import subprocess

import pytest

pytest.importorskip("networkx")

from gateway import graph as graphmod, manifest
from gateway.codegraph import build_graph


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _built(tmp_path, *, commit=True):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    if commit:
        _git(src, "init", "-b", "main")
        _git(src, "config", "user.email", "t@t")
        _git(src, "config", "user.name", "Tester")
        _git(src, "add", "-A")
        _git(src, "commit", "-m", "seed")
    vault = tmp_path / "vault"
    (vault / graphmod.GRAPH_DIRNAME).mkdir(parents=True)
    snap = vault / graphmod.GRAPH_DIRNAME / "default.json"
    data = build_graph(src)
    snap.write_text(json.dumps(data), encoding="utf-8")
    manifest.write(snap, data["graph"], src, "9.9.9")
    return vault, snap, src


def test_manifest_records_the_revision_and_holds_no_absolute_path(tmp_path):
    vault, snap, src = _built(tmp_path)
    head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    prov = manifest.read(snap)
    assert prov["status"] == "ok" and prov["revision"] == head and prov["dirty"] is False
    assert prov["snapshot_matches"] is True
    text = manifest.path_for(snap).read_text()
    assert str(tmp_path) not in text        # returned to clients, so no server paths in it
    assert "src" in text


def test_a_snapshot_changed_after_its_manifest_says_so(tmp_path):
    vault, snap, _ = _built(tmp_path)
    data = json.loads(snap.read_text())
    data["nodes"].append({"id": "smuggled"})
    snap.write_text(json.dumps(data), encoding="utf-8")
    assert manifest.read(snap)["snapshot_matches"] is False
    assert graphmod.stats(vault, "default")["provenance"]["snapshot_matches"] is False


def test_missing_and_unreadable_manifests_are_distinguishable(tmp_path):
    vault, snap, _ = _built(tmp_path)
    side = manifest.path_for(snap)
    side.unlink()
    assert graphmod.stats(vault, "default")["provenance"] == {"status": "missing"}
    side.write_text(": : not yaml\n  - [", encoding="utf-8")
    assert graphmod.stats(vault, "default")["provenance"]["status"] == "unreadable"


def test_a_source_tree_outside_git_has_no_revision(tmp_path):
    _, snap, _ = _built(tmp_path, commit=False)
    prov = manifest.read(snap)
    assert prov["status"] == "ok" and prov["revision"] is None and prov["dirty"] is None


def test_list_graphs_carries_the_revision(tmp_path):
    vault, snap, src = _built(tmp_path)
    (row,) = graphmod.list_graphs(vault)
    assert row["name"] == "default" and row["provenance"] == "ok"
    assert row["revision"] == manifest.read(snap)["revision"]
