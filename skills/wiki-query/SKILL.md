---
name: wiki-query
description: Answer questions from a git-backed Markdown or Obsidian vault through knowledge-gateway instead of guessing. Use for prior decisions, system ownership, operating procedures, project context, or locating where a subject is documented.
---

# Wiki query

Use the vault as the first source of durable project context. This is a read-only workflow.

## Procedure

1. Call `list_vaults` and select the relevant vault.
2. Call `list_notes` at the vault root. Prefer an overview, index, map of content, current-context note, or repository-specific instructions when present.
3. Narrow the search with:
   - `search` for literal or regex content;
   - `query_notes` for frontmatter `type` or `tag`;
   - `backlinks` to find notes that reference a page;
   - `list_tags` to discover the vault taxonomy.
4. Read the smallest set of load-bearing notes with `read_note`.
5. Reconcile dates, status, superseding decisions, and links. Prefer active/current notes over archived or explicitly stale content.
6. Answer with traceable references using vault-relative paths or `[[wikilinks]]`.

## Rules

- Do not invent missing facts or silently replace vault terminology with generic knowledge.
- Distinguish facts stated in the vault from inference based on several pages.
- If notes conflict, report the conflict and each note's status/date.
- If the vault is insufficient, say what was searched and which evidence is missing. Then inspect code or external sources only when the task allows it.
- Do not write during query-only work. Hand durable new findings to `wiki-curate`.
