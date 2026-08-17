# knowledge-gateway agent skills

These skills are reusable, provider-neutral operating playbooks for knowledge-gateway and its MCP tools. The canonical source is `skills/<name>/SKILL.md`; `.agents/skills` and `.claude/skills` are discovery aliases. Other repositories copy selected directories into a supported project-level path. See [`docs/agent-skills.md`](../docs/agent-skills.md) for the dated, sourced compatibility table.

| Skill | Purpose |
|---|---|
| `gateway-setup` | Configure and verify core, graph and conversion capabilities. |
| `gateway-operations` | Operate servers and publish verified releases. |
| `code-graph-build` | Detect, build and validate a deterministic repository graph. |
| `code-graph-explore` | Query graph nodes, relations, hotspots and paths. |
| `code-impact` | Estimate change blast radius before implementation. |
| `wiki-query` | Answer from the vault with traceable evidence. |
| `wiki-curate` | Record durable engineering knowledge safely. |
| `wiki-ingest` | Turn source files and attachments into structured pages. |
| `wiki-lint` | Validate vault consistency before merge. |
| `wiki-fold` | Compact old log entries without losing history. |
| `document-convert` | Convert vault-contained documents and review Markdown fidelity. |
| `canvas` | Build and edit Obsidian Canvas maps. |
| `obsidian-markdown` | Author correct Obsidian-flavored Markdown. |
| `cordis-composability` | Review reversible effects and declared dependencies. |

Preflight and copy the complete pack without trailing directory slashes so the command behaves consistently on macOS/BSD and Linux:

```bash
skill_dest=../consumer/.agents/skills
mkdir -p "$skill_dest"
for skill_dir in skills/*/; do
  skill_name=$(basename "$skill_dir")
  if [ -e "$skill_dest/$skill_name" ] || [ -L "$skill_dest/$skill_name" ]; then
    echo "refusing to overwrite $skill_name" >&2
    exit 1
  fi
done
for skill_dir in skills/*/; do
  cp -R "${skill_dir%/}" "$skill_dest/"
done
```

For a subset, name only the required directories:

```bash
cp -R skills/wiki-query skills/wiki-curate ../consumer/.agents/skills/
```

Use the destination from the sourced compatibility table. Copy on a branch and review the consumer diff. The consumer owns subsequent updates and removal. Keep the whole folder when a skill contains `references/`, `scripts/`, or `assets/`; the source `manifest.json` is inventory, not a client discovery requirement.

`manifest.json` is the machine-readable inventory. Validate the package with:

```bash
python scripts/validate-skills.py
```

The skills do not add MCP tools or bypass server controls. They define safe, repeatable sequences over tools already provided by the gateway. `graph_build` remains local-only; shared-server sessions can query existing graphs but cannot scan arbitrary source trees.
