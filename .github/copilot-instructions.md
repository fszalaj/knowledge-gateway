Follow `AGENTS.md` as the repository-wide engineering contract.

Use `gateway-setup` when configuring MCP capabilities. For substantial changes, follow the end-to-end loop in `docs/agent-skills.md`. Before broad code changes, use `code-graph-build`, `code-graph-explore`, and `code-impact`; before writing durable documentation, use `wiki-query`, `wiki-curate`, and `obsidian-markdown`.

The canonical project skills live under `skills/`; `.agents/skills` and `.claude/skills` point to the same pack for native discovery. Copy selected directories into other client discovery paths. Do not fork the skill text without updating the manifest and validator.

Do not weaken path containment, symlink protection, ACL enforcement, error masking, atomic writes, repository locks, or path-scoped Git commits. Add tests for behavior changes and keep optional dependencies lazily imported.
