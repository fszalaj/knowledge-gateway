# knowledge-gateway agent skills

These skills are reusable, provider-neutral operating playbooks for the MCP tools exposed by knowledge-gateway. Claude Code discovers `.claude/skills/<name>/SKILL.md` directly. Other agents can copy or reference the same files from their supported instruction location.

| Skill | Purpose |
|---|---|
| `code-graph` | Build and interrogate a deterministic repository graph. |
| `code-impact` | Estimate change blast radius before implementation. |
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

The skills do not add MCP tools. They define safe, repeatable sequences over tools already provided by the gateway. `graph_build` remains local-only; shared server sessions can query existing graphs but cannot scan arbitrary source trees.
