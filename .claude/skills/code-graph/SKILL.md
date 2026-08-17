---
name: code-graph
description: Build, refresh, and interrogate a knowledge-gateway code graph for architecture discovery, dependency tracing, hotspot detection, and repository orientation. Use before broad refactors, when asked how modules or Ansible roles relate, or when code search alone is insufficient.
---

# Code graph

Use the gateway's deterministic graph tools before drawing architectural conclusions from filenames or text search alone. Graph extraction is local, AST/tree-sitter based, and does not call an LLM.

## Preconditions

- The gateway runs in local stdio mode when a graph must be built.
- Install `[graph]` for Python and Ansible or `[graph-all]` for the broad tree-sitter pass.
- Choose a stable graph name, usually the repository slug.

## Workflow

1. Call `list_vaults` and select the vault that will store `.graph/<name>.json`.
2. Call `list_graphs`. Reuse a recent graph only when it represents the current source tree.
3. In local mode call `graph_build(vault, source, name)` with an absolute or repository-relative source path.
4. Call `graph_stats` and record node, edge, community, language, and extractor coverage.
5. Call `god_nodes` to identify highly connected architectural hotspots. High degree is a lead, not proof of poor design.
6. Use `graph_query` to resolve candidate modules, classes, functions, roles, tasks, handlers, filters, resources, or external modules to exact node IDs.
7. Use `graph_neighbors` with `direction=in` for callers/dependants, `direction=out` for dependencies, and `direction=both` for orientation. Start at depth 1; increase only when necessary.
8. Use `graph_shortest_path` to explain how two known nodes connect.
9. Read the source files and tests referenced by `source_file` and `source_location` before making a conclusion.

## Evidence rules

- Distinguish `EXTRACTED` edges from `INFERRED` edges.
- Treat unresolved `extmodule:*` nodes as third-party or unresolved imports, not first-party files.
- The graph does not prove runtime execution, dynamic imports, reflection, dependency injection, generated code, or configuration selected only at runtime.
- Cite node IDs, relations, source files, and locations in the result.
- If extraction coverage is weak, say so and supplement with repository search and tests.

## Output

Return a compact architecture map containing:

- graph name and freshness;
- relevant nodes and relations;
- hotspot or community observations;
- source files that must be read next;
- explicit limitations or uncertain edges.

This skill is read-only except for the deliberate local `graph_build` operation that writes the graph artifact inside the selected vault.
