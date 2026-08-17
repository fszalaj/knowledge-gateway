---
name: wiki-fold
description: Compact the oldest entries of a long vault operation log into a dated fold page while preserving history and traceability. Use when a log becomes difficult to scan or when asked to roll up or archive old entries.
---

# Wiki fold

A fold is extractive and idempotent. It shortens the active log without inventing a summary or losing original dates and outcomes.

## Gateway tools

Use `read_note`, `write_note`, `patch_note`, `git_status`, and `git_commit`.

## Procedure

1. Read the active log and existing fold pages.
2. Select only the oldest inactive tail. Keep recent sprint, incident, migration, and unresolved-decision entries live.
3. Choose a non-overlapping date range and deterministic fold path based on the vault convention.
4. Create one fold page containing:
   - frontmatter with type, coverage range, created date, and tags;
   - the exact date and title of each folded entry;
   - its existing one-line outcome or insight, restated without adding facts;
   - a link back to the active log.
5. Remove only those entries from the active log and add a pointer to the fold page.
6. Update the index/map of content when fold pages are cataloged there.
7. Run `wiki-lint`, inspect `git_status`, and commit the fold page and log change together when requested.

## Guardrails

- Never fold an entry already covered by another fold.
- Never merge unrelated events into a new interpretation.
- Never discard unresolved actions, risks, or decision context.
- If the log format is inconsistent, report the ambiguity instead of deleting content.
