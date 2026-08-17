---
name: code-impact
description: Estimate the blast radius of a proposed code, API, Terraform, or Ansible change with the knowledge-gateway graph plus source and test verification. Use before implementation, refactoring, deleting a symbol, changing an interface, or modifying a shared role or handler.
---

# Code impact analysis

Produce an evidence-backed change plan. The graph narrows the search; source, configuration, tests, and runtime behavior remain the final authority.

## Gateway tools

Use `graph_query`, `graph_neighbors`, `graph_shortest_path`, and `graph_stats`.

## Procedure

1. State the proposed change precisely: target symbol or module, behavior, compatibility expectation, and deployment boundary.
2. Run `code-graph-build` or confirm the existing graph has current provenance, then use `code-graph-explore` for orientation.
3. Resolve the target with `graph_query`. If several nodes match, inspect all candidates before selecting one.
4. Inspect direct outgoing dependencies with `graph_neighbors(direction=out, depth=1)`.
5. Inspect direct incoming dependants with `graph_neighbors(direction=in, depth=1)`.
6. Expand to depth 2 only for shared modules, public interfaces, Ansible roles, handlers, filter plugins, Terraform resources, or other cross-cutting nodes.
7. Use `graph_shortest_path` for suspected transitive connections that are not direct. It uses an undirected view; verify direction with `graph_neighbors`.
8. Read every load-bearing source file and relevant tests. Search for dynamic references, strings, configuration keys, generated clients, schemas, migrations, documentation, and CI/deployment coupling that the graph may not model.
9. Classify the impact:
   - **required changes** - compile/runtime breakage or incorrect behavior without them;
   - **compatibility risks** - public API, data, schema, state, or rollout concerns;
   - **validation scope** - unit, integration, end-to-end, migration, security, and operational checks;
   - **documentation impact** - README, runbooks, ADRs, or vault pages.
10. After implementation, rebuild the graph when topology changed and verify that intended edges changed while unrelated areas stayed stable.

## Guardrails

- Do not claim completeness from graph degree alone.
- Do not delete an apparently unused node until text search, configuration, plugin loading, entry points, and tests confirm it is unused.
- For security boundaries, inspect authorization and path validation manually even when no graph edge exists.
- For database or infrastructure changes, include forward and rollback/compatibility strategy.

## Output template

```text
Target:
Direct dependants:
Direct dependencies:
Transitive or dynamic risks:
Files to change:
Tests and quality gates:
Rollout/rollback:
Residual uncertainty:
```
