---
name: wiki-lint
description: Health-check a knowledge-gateway vault before a documentation pull request or after batch curation. Detect broken embeds, malformed or missing frontmatter, duplicate canonical pages, catalog drift, stale status, and unsafe pending Git changes.
---

# Wiki lint

Use the repository's deterministic consistency script when one exists, then supplement it with gateway queries. Report findings by severity and do not rewrite content merely to satisfy a cosmetic preference.

## Deterministic gate

1. Locate the repository's documented wiki validation command.
2. Run it exactly as documented. Common examples are `python3 scripts/wiki-consistency.py <vault>` or a project-specific lint target.
3. A failing broken embed, malformed frontmatter, invalid path, or schema error must be fixed before merge.

## Gateway health pass

- `list_notes` - inspect unexpected paths and catalog scope.
- `query_notes` - find pages by type/tag and identify missing or inconsistent metadata.
- `list_tags` - detect taxonomy drift and near-duplicate tags.
- `backlinks` - inspect orphaned canonical pages and risky renames.
- `search` - detect placeholder markers, stale status text, duplicate titles, unresolved conflict markers, or absolute local paths.
- `list_attachments` - compare attachments with embeds; treat unreferenced archives as warnings unless local policy forbids them.
- `git_status` - ensure only intended vault files are pending.

## Severity

- **Error** - broken embed, invalid structure, unsafe path, malformed frontmatter, unresolved conflict marker, missing required canonical page, or failed deterministic check.
- **Warning** - orphan attachment, stale current-context page, duplicate concept, catalog drift, inconsistent tags, or ambiguous status.
- **Info** - optional cleanup or readability improvement.

## Rules

- Do not treat every red `[[wikilink]]` as an error; some vaults intentionally use future-page links.
- Do not refresh another contributor's personal context page.
- Do not delete orphaned files automatically when provenance or archive value is unclear.
- Finish with the exact commands run, counts by severity, files changed, and residual warnings.
