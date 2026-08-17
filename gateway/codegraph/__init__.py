"""codegraph - build a queryable multi-language code knowledge graph from a source tree.

Our own extractor (no third-party graph tool): stdlib `ast` for Python, PyYAML for
Ansible, and an optional generic tree-sitter pass for more languages (JS/TS/Go/
Terraform/bash/PowerShell/... via the [graph-all] extra). Output is a NetworkX
node-link `graph.json` the gateway serves read-only. AST-only: no LLM, no network.

Node:  {id, label, type, file_type, source_file, source_location?, community?}
Edge:  {source, target, relation, confidence(EXTRACTED|INFERRED|AMBIGUOUS), confidence_score?}
"""
from __future__ import annotations

from .build import build_graph, SCHEMA_VERSION

__all__ = ["build_graph", "SCHEMA_VERSION"]
