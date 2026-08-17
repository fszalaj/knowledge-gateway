Follow `AGENTS.md` as the repository-wide engineering contract.

Use `gateway-setup` when configuring MCP capabilities. For substantial changes, follow the `knowledge-workflow` harness. Before broad code changes, use `code-graph` and `code-impact`; before writing durable documentation, use `wiki-query`, `wiki-curate`, and `obsidian-markdown`.

The canonical project skills live under `.claude/skills/`; `.agents/skills` points to the same pack for compatible clients. Do not duplicate or fork the skill text without updating the manifest and validator.

Do not weaken path containment, symlink protection, ACL enforcement, error masking, atomic writes, repository locks, or path-scoped Git commits. Add tests for behavior changes and keep optional dependencies lazily imported.
