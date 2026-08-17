Follow `AGENTS.md` as the repository-wide engineering contract.

Before broad code changes, use the `code-graph` and `code-impact` playbooks under `.claude/skills/`. Before writing durable documentation, use `wiki-query`, `wiki-curate`, and `obsidian-markdown`.

Do not weaken path containment, symlink protection, ACL enforcement, error masking, atomic writes, repository locks, or path-scoped Git commits. Add tests for behavior changes and keep optional dependencies lazily imported.
