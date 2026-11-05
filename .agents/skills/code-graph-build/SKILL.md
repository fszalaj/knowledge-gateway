---
name: code-graph-build
description: Detect, build, refresh, and validate a knowledge-gateway code graph for a local source tree. Use when no graph exists, graph provenance does not match the current revision, or a structural change makes the stored graph stale.
---

# Build a code graph

Use the gateway's deterministic local graph builder. Extraction is AST/tree-sitter based and does not call an LLM.

## Preconditions

- The gateway runs in local stdio mode when a graph must be built.
- Install `[graph]` for every supported language, or `[graph-slim]` for Python and Ansible only.
- Choose a stable graph name, usually the repository slug.
- `graph_build` is exposed only in local stdio mode. A shared HTTP server can query stored graphs but cannot scan an arbitrary source tree.

## Workflow

1. Call `list_vaults` and select the vault that will store `.graph/<name>.json`.
2. Call `list_graphs` to detect existing graphs. Each row carries the `revision` its snapshot was built from, the build time, and a `provenance` status.
3. Compare that revision with the current source revision, and check `graph_stats` provenance: `missing` or `unreadable` means freshness is unknown, and `snapshot_matches: false` means the snapshot changed after its manifest was written. Rebuild in every one of those cases.
4. Select `[graph]` when the broad tree-sitter pass is required, or `[graph-slim]` for Python and Ansible extraction only.
5. In local mode call `graph_build(vault, source, name)` with an absolute or repository-relative source path.
6. Call `graph_stats` and record node, edge, community, Python-file, tree-sitter-file, and tree-sitter-availability metadata, plus the provenance block. Building writes the `<name>.meta.yaml` sidecar next to the snapshot; do not hand-edit it, rebuild instead.
7. Treat zero or unexpectedly low counts as a failed or incomplete build. Inspect direct source coverage before accepting the artifact.
8. Hand the validated graph name and current source revision to `code-graph-explore` or `code-impact`.

## Validation rules

- Building writes `.graph/<name>.json` and its `<name>.meta.yaml` sidecar inside the selected vault; it does not change the source tree.
- The sidecar holds no absolute path: it records the source directory's basename, its git revision when there is one, and the snapshot's SHA-256.
- Do not claim language-level coverage that `graph_stats` does not report.
- A graph can be structurally valid and still incomplete for dynamic imports, reflection, dependency injection, generated code, or runtime-selected configuration.
- If tree-sitter is unavailable while broad-language coverage is required, install `[graph]` (not `[graph-slim]`) and rebuild.

### Comparing a rebuild with the snapshot it replaces

- Match node ids exactly, never by substring. A short symbol name such as `page` or `index` occurs across most of a repository, so a substring check answers "still present" about a different symbol entirely.
- A tree-sitter node id ends in `#L<line>`, so inserting one line renumbers every definition below it. A diff of node ids then shows deletions and additions where the code only moved. Compare by file and name before concluding that a symbol disappeared.
- A count that moved is a question, not a verdict: an import resolved to a first-party file adds edges, and an extractor fix can legitimately remove nodes. Establish which change explains the delta before accepting or rejecting the rebuild.
- When a rebuild and a manifest disagree, suspect the check first. The manifest was written by the thing that produced the file.

## Output

Return:

- vault and graph name;
- source path and independently recorded source revision;
- selected extra and extractor availability;
- `graph_stats` counts;
- provenance/freshness decision;
- coverage limitations and next workflow.

This skill changes only the deliberate graph artifact through `graph_build`.
