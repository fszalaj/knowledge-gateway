"""Build a NetworkX code/Ansible graph from a source tree and return node-link JSON.

Passes: Ansible (PyYAML, repo-level) + Python (ast, per file) + optional broad
languages (tree-sitter, per file). Communities (greedy modularity) are baked onto
nodes. AST-only, read-only on the source tree. networkx is imported lazily so this
module is importable without the [graph] extra.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import extract_ansible, extract_python, treesitter

SCHEMA_VERSION = 1
_PRUNE = extract_ansible.PRUNE  # one prune set shared by the Ansible pass and the file walk


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE]  # prune in place: do not descend
        for fn in filenames:
            yield Path(dirpath) / fn


def build_graph(root, languages: list[str] | None = None) -> dict:
    """Build the graph for `root`. Returns NetworkX node-link data (with a `graph`
    metadata block). `languages` optionally restricts the broad tree-sitter pass."""
    try:
        import networkx as nx
        from networkx.algorithms.community import greedy_modularity_communities
    except ImportError:
        raise ValueError("graph_unavailable: install the [graph] extra (networkx) to build graphs")

    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"not_found: {root}")

    fragments: list[dict] = [extract_ansible.extract(root)]
    n_py = n_ts = 0
    for p in _iter_files(root):
        rel = p.relative_to(root).as_posix()
        if p.suffix == ".py":
            fragments.append(extract_python.extract(p, rel))
            n_py += 1
        elif p.suffix.lower() in treesitter.LANG_EXTS:
            if languages and treesitter.EXT_LANG.get(p.suffix.lower()) not in languages:
                continue
            frag = treesitter.extract(p, rel)
            if frag["nodes"]:
                n_ts += 1
            fragments.append(frag)

    G = nx.DiGraph()
    for frag in fragments:
        for n in frag["nodes"]:
            nid = n["id"]
            if nid in G:
                G.nodes[nid].update({k: v for k, v in n.items() if v is not None and k != "id"})
            else:
                G.add_node(nid, **{k: v for k, v in n.items() if k != "id"})
        for e in frag["edges"]:
            for end in (e["source"], e["target"]):
                if end not in G:
                    G.add_node(end)
            G.add_edge(e["source"], e["target"],
                       **{k: v for k, v in e.items() if k not in ("source", "target")})

    communities: list = []
    if G.number_of_nodes():
        try:
            communities = list(greedy_modularity_communities(G.to_undirected()))
        except Exception:
            communities = []
        for cid, members in enumerate(communities):
            for nid in members:
                G.nodes[nid]["community"] = cid

    G.graph.update({
        "schema_version": SCHEMA_VERSION,
        "root": root.name,
        "files_python": n_py,
        "files_treesitter": n_ts,
        "treesitter_available": treesitter.AVAILABLE,
        "communities": len(communities),
        "node_count": G.number_of_nodes(),
        "edge_count": G.number_of_edges(),
    })
    return nx.node_link_data(G, edges="links")
