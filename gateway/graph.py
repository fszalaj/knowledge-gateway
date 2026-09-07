"""Read-only query layer over a built code graph (`<vault>/.graph/<name>.json`).

The MCP graph tools call these helpers. networkx is an optional dependency (the
[graph] extra); importing this module without it raises a clean, masked error only
when a graph tool is actually used - the vault tools never touch it.

Containment: graph files live in the vault's `.graph/` dir; every path is resolved and
checked to stay inside the vault (a symlinked `.graph` or graph file cannot escape).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import manifest

GRAPH_DIRNAME = ".graph"


def _nx():
    try:
        import networkx as nx
        return nx
    except ImportError:  # pragma: no cover
        raise ValueError("graph_unavailable: install the [graph] extra (networkx) to use graph tools")


def graph_file(vault_path: Path, name: str, *, must_exist: bool = True) -> Path:
    """Resolve <vault>/.graph/<name>.json, sanitised and contained inside the vault."""
    safe = name.strip().removesuffix(".json")
    if not safe or "/" in safe or "\\" in safe or safe.startswith("."):
        raise ValueError(f"graph_invalid: bad graph name {name!r}")
    base = Path(vault_path).resolve()
    p = base / GRAPH_DIRNAME / f"{safe}.json"
    # resolve() follows symlinks; reject a .graph or graph file that escapes the vault.
    if not p.resolve().is_relative_to(base):
        raise PermissionError(f"path_escape: {name}")
    if must_exist and not p.is_file():
        raise FileNotFoundError(f"graph_not_found: {name}")
    return p


def list_graphs(vault_path: Path) -> list[dict]:
    """Graphs in the vault, each with its counts and the provenance of its snapshot."""
    base = Path(vault_path).resolve()
    d = base / GRAPH_DIRNAME
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        if not p.resolve().is_relative_to(base):  # skip symlinked-out files
            continue
        try:
            g = json.loads(p.read_text(encoding="utf-8"))
            meta = g.get("graph", {}) if isinstance(g, dict) else {}
        except Exception:
            meta = {}
        prov = manifest.read(p)
        out.append({"name": p.stem, "nodes": meta.get("node_count"),
                    "edges": meta.get("edge_count"), "communities": meta.get("communities"),
                    "revision": prov.get("revision"), "built_at": prov.get("built_at"),
                    "provenance": prov["status"]})
    return out


def load(vault_path: Path, name: str):
    nx = _nx()
    p = graph_file(vault_path, name)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return nx.node_link_graph(data, edges="links")
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"graph_invalid: {name}: {e}")


def _node_view(G, nid: str) -> dict:
    d = G.nodes[nid]
    return {"id": nid, "label": d.get("label"), "type": d.get("type"),
            "file_type": d.get("file_type"), "source_file": d.get("source_file"),
            "source_location": d.get("source_location"), "community": d.get("community"),
            "degree": G.degree(nid)}


def query(vault_path: Path, name: str, text: str, limit: int = 50) -> list[dict]:
    G = load(vault_path, name)
    t = (text or "").lower()
    hits = [nid for nid, d in G.nodes(data=True)
            if t in nid.lower() or t in str(d.get("label", "")).lower()]
    hits.sort(key=lambda n: G.degree(n), reverse=True)
    return [_node_view(G, n) for n in hits[: max(1, min(limit, 500))]]


def neighbors(vault_path: Path, name: str, node_id: str, depth: int = 1,
              direction: str = "both", limit: int = 100) -> dict:
    G = load(vault_path, name)
    if node_id not in G:
        raise ValueError(f"node_not_found: {node_id}")
    depth = max(1, min(depth, 4))
    seen = {node_id}
    order = [node_id]          # insertion order, centre first: a set would truncate at random
    frontier = [node_id]
    edges: dict[tuple, dict] = {}   # keyed: depth>=2 reaches the same edge from both ends
    for _ in range(depth):
        nxt = []
        for n in frontier:
            pairs = []
            if direction in ("out", "both"):
                pairs += [(n, s) for s in G.successors(n)]
            if direction in ("in", "both"):
                pairs += [(p, n) for p in G.predecessors(n)]
            for u, v in pairs:
                edges.setdefault((u, v), {"source": u, "target": v,
                                          "relation": G.edges[u, v].get("relation"),
                                          "confidence": G.edges[u, v].get("confidence")})
                other = v if u == n else u
                if other not in seen:
                    seen.add(other)
                    order.append(other)
                    nxt.append(other)
        frontier = nxt
        if not frontier:
            break
    cap = max(1, min(limit, 500))
    edge_cap = max(1, min(limit * 4, 2000))
    nodes = [_node_view(G, n) for n in order[:cap]]
    return {"center": node_id, "nodes": nodes,
            "truncated": len(order) > cap or len(edges) > edge_cap,
            "edges": list(edges.values())[:edge_cap]}


def god_nodes(vault_path: Path, name: str, top_n: int = 10) -> list[dict]:
    G = load(vault_path, name)
    ranked = sorted(G.nodes(), key=lambda n: G.degree(n), reverse=True)
    return [_node_view(G, n) for n in ranked[: max(1, min(top_n, 100))]]


def shortest_path(vault_path: Path, name: str, source: str, target: str) -> dict:
    nx = _nx()
    G = load(vault_path, name)
    if source not in G:
        raise ValueError(f"node_not_found: {source}")
    if target not in G:
        raise ValueError(f"node_not_found: {target}")
    try:
        path = nx.shortest_path(G.to_undirected(), source, target)
    except nx.NetworkXNoPath:
        raise ValueError(f"no_path: {source} -> {target}")
    return {"path": path, "length": len(path) - 1}


def stats(vault_path: Path, name: str) -> dict:
    p = graph_file(vault_path, name)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"graph_invalid: {name}: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list) or not isinstance(data.get("links"), list):
        raise ValueError(f"graph_invalid: {name}: not a node-link graph")
    # A snapshot cannot refuse to be stale, so what it was built from travels with it.
    return {**data.get("graph", {}), "provenance": manifest.read(p)}
