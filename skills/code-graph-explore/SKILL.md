---
name: code-graph-explore
description: Explore an existing knowledge-gateway code or Ansible graph using node lookup, neighbours, hotspots, paths, relation types, and source locations. Use for architecture discovery, dependency tracing, repository orientation, or explaining how two components connect.
---

# Explore a code graph

Use a validated graph as a navigation aid, then confirm every load-bearing conclusion in source and tests.

## Procedure

1. Call `list_graphs` and select the intended graph. Call `graph_stats` and state its known provenance limits.
2. Call `graph_query` to resolve modules, classes, functions, methods, roles, tasks, handlers, filters, resources, or external modules. Keep every plausible match until the source disambiguates it.
3. Record exact node IDs. Python uses `module:`, `pyclass:`, `pyfunc:`, and `pymethod:`; Ansible adds `role:`, `tasksfile:`, `task:`, `handler:`, `playbook:`, and `filter:`. Tree-sitter definitions use `<language>:<path>:<name>#L<line>`. Unresolved imports use `extmodule:`.
4. Call `graph_neighbors(direction=out, depth=1)` for dependencies and `graph_neighbors(direction=in, depth=1)` for dependants. Use `both` for orientation and increase depth only when necessary.
5. Read each returned `relation` and `confidence`. Current extractors emit `EXTRACTED` and `INFERRED`; the graph schema reserves `AMBIGUOUS` for future or imported data. An inferred call is weaker than a parsed definition or import.
6. Call `god_nodes` to find highly connected starting points. High degree is a lead, not proof of poor design or ownership.
7. Call `graph_shortest_path` only after resolving exact node IDs. It searches an undirected view, so it proves a connection, not directional reachability. Confirm direction with `graph_neighbors`.
8. Read the files and locations returned as `source_file` and `source_location`, then inspect tests, configuration, generated code, and runtime wiring that the graph may not model.

## Evidence rules

- Treat unresolved `extmodule:*` nodes as external or unresolved imports, not confirmed first-party files.
- Do not infer runtime execution from a static edge.
- Do not claim completeness for reflection, dynamic imports, dependency injection, generated code, templating, or runtime-selected configuration.
- If extraction coverage is weak or a node is missing, supplement with repository search and say so.

Return the graph and provenance, exact node IDs, directional relations, confidence, hotspot/path observations, source evidence, and residual blind spots.
