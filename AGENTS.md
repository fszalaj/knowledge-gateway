# Agent engineering guide

This repository is a security-sensitive Python/FastMCP service. Treat the vault and code-graph layers as trust boundaries, not convenience wrappers.

## Start here

1. Read `README.md`, then the relevant implementation and tests.
2. For architecture or impact questions, use the bundled skills under `.claude/skills/`.
3. Build or refresh a local code graph before broad refactors when the `[graph]` or `[graph-all]` extra is available.
4. Keep changes narrow. Do not combine unrelated refactors, dependency upgrades, and documentation changes.

## Required engineering practices

- Work on a short-lived branch and open a pull request. Do not push directly to `main` or move `stable` as part of ordinary development.
- Preserve local/server mode parity unless the difference is an explicit security boundary.
- Keep every filesystem path contained within its configured vault. Never weaken traversal, symlink, hidden-file, attachment-size, or ACL checks.
- Mutating operations must remain atomic, lock-protected, pathspec-scoped, and attributable to the requesting identity.
- Unexpected server-side errors must stay masked in shared HTTP mode. Only deliberate gateway errors may reach clients.
- Optional capabilities must stay lazily imported so the core package remains usable without graph or conversion extras.
- Add or update tests for behavioral changes. Run the full test suite before claiming completion.
- Update `README.md` and `CHANGELOG.md` when user-visible behavior or operating procedures change.

## Validation

```bash
uv lock --check
uv venv
uv pip install -e ".[dev]"
uv run python scripts/validate-skills.py
uv run pytest -q
```

For graph-related work, also inspect at least one built graph with `graph_stats`, `god_nodes`, `graph_query`, and `graph_neighbors`. For vault mutations, test both successful writes and rejected traversal/ACL cases.

## Agent skills

Canonical skill files live in `.claude/skills/<name>/SKILL.md`. They are provider-neutral operating playbooks even though Claude Code discovers this location natively. Other agents can copy or reference the same files from their supported instruction directory.

Use:

- `code-graph` to build and inspect a repository graph.
- `code-impact` before changes that may cross modules, roles, handlers, or imported packages.
- `wiki-query` to answer from the vault rather than guess.
- `wiki-curate` after durable decisions or implementation changes.
- `wiki-ingest` to convert source material into structured knowledge.
- `wiki-lint` before a wiki-related pull request.
- `wiki-fold` to compact an operation log without losing history.
- `canvas` for Obsidian Canvas maps.
- `obsidian-markdown` whenever authoring vault pages.

The manifest at `.claude/skills/manifest.json` is the machine-readable inventory and validation source.
