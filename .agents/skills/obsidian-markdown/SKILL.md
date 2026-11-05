---
name: obsidian-markdown
description: Author valid Obsidian-flavored Markdown for a knowledge-gateway vault, including wikilinks, embeds, frontmatter, callouts, tags, headings, and block references. Consult before creating or editing vault notes.
---

# Obsidian-flavored Markdown

Follow the vault's templates and local conventions first. These rules cover the portable baseline.

## Internal links and embeds

- Wikilink: `[[Note Name]]`
- Alias: `[[Note Name|display text]]`
- Heading: `[[Note Name#Heading]]`
- Block: `[[Note Name#^block-id]]`
- Embed/transclusion: `![[Note Name]]` or `![[image.png]]`

Prefer wikilinks for internal knowledge. Use normal Markdown links for external URLs. Attachment files must live inside the vault.

## Frontmatter

Place YAML frontmatter at the very top between `---` fences. Reuse keys and value formats from `_templates/<type>.md`.

```yaml
---
type: decision
status: active
created: 2026-08-17
updated: 2026-08-17
tags: [gateway, architecture]
---
```

Keep `tags` consistently typed as a list when that is the vault convention. Use ISO dates.

## Callouts

```text
> [!note] Optional title
> Body text.
```

Common types: `note`, `tip`, `warning`, `danger`, `info`, `success`, `question`, `example`, `todo`, and `bug`. Use foldable callouts only when they improve scanning.

## Authoring rules

- One top-level `#` heading per page unless a template says otherwise.
- Preserve exact identifiers and code in backticks.
- Keep headings stable because links may target them.
- Use fenced code blocks with a language identifier.
- Use `patch_frontmatter` for metadata-only changes and `patch_note` for bounded body additions.
- Check `backlinks` before renaming a page or heading.
- Do not edit `.canvas` as Markdown; use the `canvas` skill.
- Do not reference `/tmp`, a developer home directory, or files outside the vault.
