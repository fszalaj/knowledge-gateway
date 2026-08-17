---
name: knowledge-workflow
description: Run a complete engineering harness that combines wiki context, code-graph evidence, explicit specification, bounded implementation, deterministic quality gates, independent review, and post-change curation. Use for substantial features, refactors, migrations, and operational changes.
---

# Knowledge-driven engineering workflow

The gateway is the context and evidence layer. Repository tests, hooks, CI and server-side controls remain the enforcement layer.

## Stages

1. **Discover**
   - Call `list_vaults` and use the `wiki-query` skill for architecture, decisions, constraints and operating history.
   - Call `graph_stats`, `graph_query` and `graph_neighbors`, or use the `code-graph` skill, for relevant modules, call paths, Ansible roles and infrastructure relationships.
2. **Specify**
   - Write scope, assumptions, acceptance criteria, compatibility, security boundaries, observability, rollout and rollback.
   - Use `code-impact` before a high-risk interface, schema, role, handler or shared-module change.
3. **Implement**
   - Keep the implementation bounded to the accepted specification.
   - Separate the implementer context from final review; do not let an agent approve its own unverified assumptions.
4. **Validate**
   - Run the repository's format, lint, type, unit, integration, end-to-end, security and build gates.
   - Verify the deployed revision and health checks when deployment is in scope.
5. **Independent review**
   - Use a separate reviewer agent or model to challenge assumptions, inspect the diff and test failure and rollback paths.
   - Verify review findings instead of accepting them automatically.
6. **Curate**
   - Use `patch_note` or `patch_frontmatter` through `wiki-curate` to record durable decisions, architecture, runbooks and lessons.
   - Rebuild the code graph when committed topology changed materially.
7. **Close**
   - Call `git_status` and map every acceptance criterion to evidence.

## Completion gate

Do not declare completion until:

- acceptance criteria are mapped to evidence;
- deterministic quality gates pass;
- security, compatibility and rollback were considered;
- the deployed revision is verified when applicable;
- durable knowledge is updated, or an explicit reason for no wiki change is recorded.
