---
name: canvas
description: Create or edit Obsidian Canvas maps through knowledge-gateway using stable node IDs, non-overlapping layout, groups, colors, and labeled edges. Use for architecture maps, knowledge maps, process flows, or when asked to place vault pages on a canvas.
---

# Canvas

Obsidian `.canvas` files are JSON Canvas documents. Use `read_canvas` and `write_canvas`; do not perform unreviewed text replacement of the JSON file.

## Model

Each node has `id`, `type`, `x`, `y`, `width`, and `height`, with optional `color`.

- `text` nodes contain Markdown in `text`.
- `file` nodes reference a vault-relative `file` and may include a `subpath`.
- `link` nodes contain a `url`.
- `group` nodes are labeled visual containers.

Each edge has `id`, `fromNode`, `toNode`, and may include side anchors, color, and label.

## Workflow

1. Call `list_canvases` and `read_canvas`, or start with `{"nodes": [], "edges": []}` for a new file.
2. Preserve all existing nodes and edges unless removal is explicitly requested.
3. Assign stable unique IDs based on durable slugs, not array positions.
4. Prefer `file` nodes referencing canonical vault pages over duplicated prose in `text` nodes.
5. Lay out nodes without overlap. Use a grid or columns with at least 40-80 px spacing. Size a group to extend around its contained nodes.
6. Connect only meaningful relationships and label ambiguous edges.
7. Call `write_canvas` with the complete object.
8. Re-read the canvas and verify node IDs, edge endpoints, file paths, layout bounds, and counts.
9. Commit only when requested or required by the repository workflow.

## Rules

- Reference only files inside the vault.
- Do not use temporary local paths for images or attachments.
- Use colors consistently and sparingly; document a legend when color carries meaning.
- For large maps, create multiple focused canvases rather than one unreadable graph.
