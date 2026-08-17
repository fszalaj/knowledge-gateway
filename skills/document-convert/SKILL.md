---
name: document-convert
description: Convert a vault-contained PDF, Office document, image, HTML page, CSV, or other supported file to Markdown through knowledge-gateway, then review fidelity and preserve provenance. Use when source material must become readable or ingestible Markdown.
---

# Convert documents to Markdown

Use `convert_to_markdown` for a file that already exists inside the selected vault. The tool returns Markdown and does not modify the source.

Use `list_vaults` and `convert_to_markdown`. If durable storage is requested, use `write_note` and inspect `git_status` before commit.

## Preconditions

- Run the gateway with the `[convert]` extra.
- Prefer the core or graph-only install on servers that do not need conversion; PDF and Office support adds a materially larger parser dependency tree.
- Treat format support as the installed MarkItDown converter set, not as a promise that every file with a known extension will convert.
- Keep the original source immutable. Conversion is a derived representation, not a replacement.
- The gateway rejects hidden or out-of-vault paths and files over 50 MiB.

## Procedure

1. Select the vault and record the vault-relative source path, source type, and provenance.
2. Call `convert_to_markdown(vault, path)` once.
3. Review the result for missing pages, tables, headings, links, images, OCR text, encoding damage, and empty output.
4. Compare load-bearing names, dates, identifiers, requirements, and code samples with the source. Mark any uncertain extraction explicitly.
5. If durable storage is requested, create a separate Markdown note with `write_note` using the vault's template and link it to the unchanged source. Do not silently overwrite an existing canonical page.
6. Run the vault's lint or consistency check and inspect `git_status` before commit.

## Failure handling

- `convert_unavailable` means the gateway was installed without the conversion extra.
- `convert_failed` may mean the file is malformed, encrypted, unsupported, or needs a converter-specific optional dependency.
- A successful call proves only that text was produced. It does not prove semantic or visual fidelity.

Return the source path, detected limitations, output review, storage path when written, and remaining uncertainty.
