"""CLI: build a code graph from a source tree into a graph.json.

    obsidian-gateway-graph <source> -o <vault>/.graph/<name>.json [--languages js ts ...]

Runs where the code is (the source tree may be outside any vault); writes a node-link
graph.json the gateway then serves read-only. AST-only, no network, no LLM.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="knowledge-gateway-graph",
                                 description="Build a code/Ansible knowledge graph (graph.json).")
    ap.add_argument("source", help="source tree (code repo) to graph")
    ap.add_argument("-o", "--out", required=True, help="output graph.json path")
    ap.add_argument("--languages", nargs="*", default=None,
                    help="restrict the tree-sitter pass to these languages (default: all available)")
    args = ap.parse_args(argv)

    from .build import build_graph  # imports networkx; needs the [graph] extra
    data = build_graph(args.source, languages=args.languages)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    g = data.get("graph", {})
    print(f"built: {g.get('node_count')} nodes, {g.get('edge_count')} edges, "
          f"{g.get('communities')} communities (tree-sitter: {g.get('treesitter_available')}) -> {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
