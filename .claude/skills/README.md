# knowledge-gateway agent skills

These skills are reusable, provider-neutral operating playbooks for the MCP tools exposed by knowledge-gateway. Claude Code and GitHub Copilot can discover the canonical files under `.claude/skills/<name>/SKILL.md`; `.agents/skills` points to the same pack for Codex and other clients using the open Agent Skills convention.

| Skill | Purpose |
|---|---|
| `gateway-setup` | Configure and verify core, graph and conversion capabilities. |
| `code-graph` | Build and interrogate a deterministic repository graph. |
| `code-impact` | Estimate change blast radius before implementation. |
| `knowledge-workflow` | Run discovery, specification, implementation, gates, review and curation as one harness. |
| `wiki-query` | Answer from the vault with traceable evidence. |
| `wiki-curate` | Record durable engineering knowledge safely. |
| `wiki-ingest` | Turn source files and attachments into structured pages. |
| `wiki-lint` | Validate vault consistency before merge. |
| `wiki-fold` | Compact old log entries without losing history. |
| `canvas` | Build and edit Obsidian Canvas maps. |
| `obsidian-markdown` | Author correct Obsidian-flavored Markdown. |

`manifest.json` is the machine-readable inventory. Validate the package with:

```bash
python scripts/validate-skills.py
```

The skills do not add MCP tools or bypass server controls. They define safe, repeatable sequences over tools already provided by the gateway. `graph_build` remains local-only; shared-server sessions can query existing graphs but cannot scan arbitrary source trees.
