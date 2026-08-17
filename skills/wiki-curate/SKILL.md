---
name: wiki-curate
description: Keep a git-backed Markdown or Obsidian vault current after substantive engineering work by recording decisions, modules, procedures, links, current context, and an operation log. Use after a feature, refactor, incident, deployment, architectural decision, or when asked to update or save knowledge.
---

# Wiki curate

Curate durable knowledge, not a transcript of the work session. Use gateway read tools first and the smallest safe mutation tool for each change.

## Gateway tools

Use `list_vaults`, `read_note`, `search`, `backlinks`, `write_note`, `patch_note`, `patch_frontmatter`, `rename_note`, `delete_note`, `git_status`, and `git_commit`.

## Before writing

1. Select the vault with `list_vaults`.
2. Read repository or vault instructions, the index/map of content, relevant pages, and `_templates/<type>.md` when templates exist.
3. Search for an existing canonical page before creating a new one.
4. Check `backlinks` before renaming or restructuring a page.

## Write strategy

- Use `patch_note` for a bounded insertion under a known heading or at top/bottom.
- Use `patch_frontmatter` for status, ownership, tags, or `updated` changes without rewriting the body.
- Use `write_note` for a new page or a deliberate full replacement.
- Use `rename_note` rather than a filesystem move so inbound flat wikilinks are updated safely.
- Use `delete_note` only after confirming the page is obsolete and its backlinks are handled.

## What to record

- the problem or context;
- the decision or implemented behavior;
- rationale and rejected alternatives when material;
- operational or security boundaries;
- validation and rollback information;
- links to related systems, modules, decisions, sources, and runbooks;
- owner, status, created/updated dates, and tags following local templates.

Where the vault has an operation log or per-author current-context note, update them using the vault's established convention. Do not edit another contributor's personal context page.

## Finish

1. Re-read every changed note.
2. Run `wiki-lint` or the repository's deterministic consistency command.
3. Call `git_status` and review the exact vault-scoped changes.
4. Commit only when requested or when the established workflow explicitly requires it. Use a focused message such as `wiki: document <topic>`.

## Do not write when

- existing pages already answer the question and no durable fact changed;
- the information is speculative or unverified;
- the change would duplicate a canonical page;
- the only result is ephemeral progress that belongs in a task tracker rather than the knowledge base.
