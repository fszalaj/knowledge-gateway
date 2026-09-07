"""CLI: build a code graph from a source tree into a graph.json.

    knowledge-gateway-graph <source> -o <vault>/.graph/<name>.json [--languages javascript typescript ...]

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
                                 description="Build a multi-language code knowledge graph (graph.json).")
    ap.add_argument("source", help="source tree (code repo) to graph")
    ap.add_argument("-o", "--out", required=True, help="output graph.json path")
    ap.add_argument("--languages", nargs="*", default=None,
                    help="restrict the tree-sitter pass to these tree-sitter language names, "
                         "e.g. javascript typescript tsx go rust (default: all available)")
    ap.add_argument("--exclude", nargs="*", default=None,
                    help="extra directory names to skip (hidden dirs and common "
                         "build/vendor dirs are always skipped)")
    ap.add_argument("--include", nargs="*", default=None,
                    help="directory names to keep even if the prune rule would skip "
                         "them (e.g. .github, vendor)")
    args = ap.parse_args(argv)

    from .build import build_graph  # imports networkx; needs the [graph] extra
    try:
        data = build_graph(args.source, languages=args.languages,
                           exclude=args.exclude, include=args.include)
    except ValueError as e:  # an unknown --languages deserves an argparse error, not a traceback
        if not str(e).startswith("graph_invalid:"):
            raise
        ap.error(str(e).removeprefix("graph_invalid: "))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    from .. import __version__, manifest
    side = manifest.write(out, data.get("graph", {}), Path(args.source), __version__)
    g = data.get("graph", {})
    print(f"built: {g.get('node_count')} nodes, {g.get('edge_count')} edges, "
          f"{g.get('communities')} communities (tree-sitter: {g.get('treesitter_available')}) -> {out}")
    print(f"provenance: {side.name}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
