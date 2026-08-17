---
name: code-graph-build
description: Detect, build, refresh, and validate a knowledge-gateway code graph for a local source tree. Use when no graph exists, graph provenance does not match the current revision, or a structural change makes the stored graph stale.
---

# Build a code graph

Use the gateway's deterministic local graph builder. Extraction is AST/tree-sitter based and does not call an LLM.

## Preconditions

- The gateway runs in local stdio mode when a graph must be built.
- Install `[graph]` for Python and Ansible or `[graph-all]` for the broad tree-sitter pass.
- Choose a stable graph name, usually the repository slug.
- `graph_build` is exposed only in local stdio mode. A shared HTTP server can query stored graphs but cannot scan an arbitrary source tree.

## Workflow

1. Call `list_vaults` and select the vault that will store `.graph/<name>.json`.
2. Call `list_graphs` to detect existing graphs. The current metadata does not include a source revision or build timestamp, so do not infer freshness from `graph_stats` alone.
3. Compare the graph's recorded provenance, when the consuming repository keeps one, with the current source revision. Rebuild when provenance is missing or stale.
4. Select `[graph]` for Python and Ansible extraction or `[graph-all]` when the broad tree-sitter pass is required.
5. In local mode call `graph_build(vault, source, name)` with an absolute or repository-relative source path.
6. Call `graph_stats` and record node, edge, community, Python-file, tree-sitter-file, and tree-sitter-availability metadata.
7. Treat zero or unexpectedly low counts as a failed or incomplete build. Inspect direct source coverage before accepting the artifact.
8. Hand the validated graph name and current source revision to `code-graph-explore` or `code-impact`.

## Validation rules

- Building writes `.graph/<name>.json` inside the selected vault; it does not change the source tree.
- Do not claim language-level coverage that `graph_stats` does not report.
- A graph can be structurally valid and still incomplete for dynamic imports, reflection, dependency injection, generated code, or runtime-selected configuration.
- If tree-sitter is unavailable while broad-language coverage is required, install `[graph-all]` and rebuild.

## Output

Return:

- vault and graph name;
- source path and independently recorded source revision;
- selected extra and extractor availability;
- `graph_stats` counts;
- provenance/freshness decision;
- coverage limitations and next workflow.

This skill changes only the deliberate graph artifact through `graph_build`.
