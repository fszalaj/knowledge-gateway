---
name: wiki-ingest
description: Convert a source file, transcript, document, image, HTML page, or raw note into structured, cross-linked vault knowledge. Use when asked to ingest or process a source, or when an attachment should become durable documentation.
---

# Wiki ingest

Preserve the source, extract only durable information, and create structured pages that follow the vault's existing taxonomy.

## Gateway tools

Use `list_notes`, `list_attachments`, `read_note`, `read_attachment`, `convert_to_markdown`, `search`, `write_note`, `patch_note`, and `git_status`.

## Procedure

1. Select the vault and locate the source with `list_notes` or `list_attachments`.
2. Read Markdown sources with `read_note`, supported binary sources with `read_attachment`, or call `convert_to_markdown` for PDF, Office, image, HTML, CSV, and similar files when the `[convert]` extra is installed.
3. Treat raw/source directories as immutable unless local instructions explicitly say otherwise. Gateway tools deliberately cannot access hidden paths such as `.raw/`; use a separately authorized local-filesystem workflow instead of weakening that guard.
4. Extract verifiable entities, concepts, decisions, requirements, procedures, risks, owners, dates, and unresolved questions.
5. Search the vault to avoid duplicate pages and to identify canonical terminology.
6. Read the relevant `_templates/<type>.md` files when present.
7. Create or update one canonical page per distinct subject. Summarize and normalize; do not paste the full source into every page.
8. Create a source-summary page when provenance must be retained. Link derived pages back to it.
9. Add `[[wikilinks]]` between related pages and update the appropriate index or map of content.
10. Record uncertainty and conflicting statements explicitly.
11. Run `wiki-lint`, review `git_status`, and commit only according to the repository workflow.

## Quality rules

- Accuracy over volume. A small source should produce a small number of useful pages.
- Preserve names, dates, identifiers, requirements, and source terminology exactly where they are load-bearing.
- Do not promote an inference to a fact.
- Do not overwrite a newer active decision with an older source.
- Keep confidential or personal data out of pages that do not need it.
