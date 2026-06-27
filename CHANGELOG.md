# Changelog

All notable changes to knowledge-gateway. Consumers track the moving **`stable`** branch
(`uvx --refresh --from git+...@stable`); each release moves `stable` and auto-propagates on
next launch (no per-repo re-pin). Every release is also an immutable `vX.Y.Z` tag for
pinning/audit.

## v0.8.0 - 2026-06-27

### Added
- **Broad multi-language code graph (the `[graph-all]` tree-sitter pass) now works** across ~30
  languages: JS/TS/TSX, Go, Rust, Java, C#, C/C++, Ruby, PHP, bash, PowerShell, Terraform/HCL,
  Lua, Kotlin, Swift, Scala, R, Perl, Elixir, Clojure, Dart, SQL, Groovy, Julia, Solidity,
  Haskell, OCaml. Each file yields definitions (functions / methods / classes / structs /
  interfaces / traits / enums / types / namespaces + HCL resources), `defines` containment,
  `imports` edges (`extmodule:<name>`, capturing framework/library use), and within-file `calls`.

### Fixed
- The tree-sitter pass was **silently broken**: it was written for upstream py-tree-sitter
  (`parse(bytes)`, `node.type`, `node.children`) but the bundled binding exposes methods and a
  str-only `parse()`, so every file threw and the error was swallowed -> zero nodes. The extractor
  now uses portable accessors that handle both bindings, drops anonymous-arrow params being mistaken
  for defs, and bounds traversal. Added `tests/test_treesitter.py` + `tree-sitter-language-pack` in
  the `dev` extra so CI exercises the broad pass.

## v0.7.5 - 2026-06-27

### Distribution
- Published to PyPI as `knowledge-gateway` (Trusted Publishing), alongside the moving `@stable` git branch.

### CI
- Bumped `actions/checkout` to v7.0.0; refreshed the dev lockfile (pytest).

## v0.6.0 - 2026-06-27

### Added
- **Code graph (optional `[graph]` / `[graph-all]`)**: a `gateway/codegraph/` package builds a
  NetworkX graph of a source tree - Python (`ast`), Ansible (PyYAML walker: roles/tasks/handlers/
  `include_role`/`import_tasks`/`notify` + `task -> filter plugin` edges), and an optional broad
  tree-sitter pass (JS/TS/Go/Rust/Terraform/bash/PowerShell/...). New read-only MCP tools
  `list_graphs`, `graph_query`, `graph_neighbors`, `god_nodes`, `graph_shortest_path`,
  `graph_stats`; a local-only `graph_build`; and a `knowledge-gateway-graph` CLI.
- **Document conversion (optional `[convert]`)**: `convert_to_markdown` turns a vault file
  (PDF/Office/image/HTML/...) into Markdown via markitdown.

### Security
- Graph files live in the vault's `.graph/` and are vault-contained (resolved + `is_relative_to`
  the vault, so a symlinked `.graph` cannot escape). Malformed graphs map to `graph_invalid:`.
  Optional deps (networkx, tree-sitter-language-pack, markitdown) are imported lazily, so the core
  gateway requires none of them.

## v0.5.1 - 2026-06-16

### Changed
- **Server instructions**: point agents at `_templates/<type>.md` before creating a page
  (the folder is already reachable via `list_notes`/`read_note` - no new tools).

### Distribution
- **PyPI Trusted Publishing**: `release.yml` publishes to PyPI on a `vX.Y.Z` tag via OIDC
  (no token). First PyPI release - consumers can `uvx knowledge-gateway` (alongside `@stable`).
- **MCP Registry**: `server.json` manifest + a `mcp-name` marker in the README, for listing
  in the official MCP Registry.

## v0.5.0 - 2026-06-15

### Features (MCP-FEAT)
- **Attachments**: `list_attachments` + `read_attachment` - read binary vault files (images
  return as an inline `Image`; PDF/audio/video as a `File`), path-guarded, 25 MiB cap.
- **Obsidian Canvas**: `list_canvases` + `read_canvas` + `write_canvas` - read/write `.canvas`
  JSON (nodes including `group` nodes, edges, `color` fields), so agents can work with groups
  and colors.

## v0.4.2 - 2026-06-15

### Concurrency (CONC-1)
- **Per-repo write lock** (`fcntl.flock`): serializes read-modify-write tools
  (`patch_note` / `patch_frontmatter`) and concurrent commits across threads and processes on
  one host, fixing lost-update and mixed-commit races on the shared server. Taken once at the
  tool boundary (the inner commit never re-locks); degrades to a no-op (one-time warning) where
  fcntl/flock is unavailable, rather than failing the write.
- **Path-scoped commits**: each mutating op commits only its own files (`commit(paths=...)`),
  so a `commit=True` op cannot sweep and mis-attribute a concurrent op's pending change.
  `GIT_LITERAL_PATHSPECS=1` on every git call.

## v0.4.1 - 2026-06-15

### Docs
- README rewritten (enterprise style; architecture + `stable`-distribution mermaid diagrams; documents the `stable` "update once" model; AI-setup prompt fixed to `@stable` + `--local`).

### Changed
- The FastMCP server ships an `instructions` prompt describing the tools + Obsidian/git conventions to connecting agents.
- Reference deploy artifacts for the `@stable` model: `deploy/knowledge-gateway.service` (uv-tool binary) + `deploy/knowledge-gateway-update.{service,timer}` + `deploy/auto-update.sh`.

### Dependencies
- `ruamel.yaml` allowed up to `<0.20` (lock 0.19.1; Dependabot).

## v0.4.0 - 2026-06-15

### Security
- Server-mode **error masking**: the HTTP server runs `mask_error_details=True`; the
  gateway's expected client-facing failures surface as `ToolError`, while unexpected OS/git
  errors are hidden from the client.

### Features
- **`--local` vault auto-detect**: `--local` / `KNOWLEDGE_GATEWAY_LOCAL` auto-detects the
  cwd's vault (cwd-is-vault, `./wiki`, a real `*-obsidian-vault`, a child with `.obsidian/`),
  so one global codex/antigravity MCP config works in any repo. Explicit `--vault` still
  wins; a bare invocation still runs the HTTP server.

### Distribution
- Introduced the moving **`stable`** branch for "update once" rollout (see the header).

## v0.3.0 - 2026-06-15

Security, supply-chain, Obsidian-correctness and test coverage. Re-baselines the project
after the old `v0.2.0` tag was removed; pin this release's commit SHA (or a future PyPI
`==0.3.0`).

### Security & supply chain
- Runtime deps bounded to the current major (`fastmcp>=3,<4`, `pyyaml>=6,<7`, `ruamel.yaml>=0.18,<0.19`); `uv.lock` committed and CI-verified (`uv lock --check`).
- `tokens.yaml` is refused at load time if it is group/world-readable.
- `atomic_write` preserves an existing note's file mode (a new note is 0644, not mkstemp's 0600).
- systemd unit hardened with seccomp/capability/rlimit sandboxing (safe for a `--user` unit).
- CI: Node 24 SHA-pinned actions, Python 3.11-3.13 matrix, ripgrep installed, coverage; Dependabot for github-actions + uv.

### Features & correctness
- `backlinks` and `rename_note` match the flat note name **case-insensitively** (Obsidian resolves links that way) and accept a trailing `.md` and `^block`.
- `read_note` rejects a note over 10 MiB; `query_notes` handles a scalar frontmatter `tags:`.
- `__version__` is sourced from package metadata.

### Tests
- Coverage 54% -> 82%; added end-to-end tool tests plus rename / gitops / search / security suites.

### Planned
- PyPI Trusted Publishing, so consumers can `uvx knowledge-gateway==<version>` without a git fetch.
- Server-mode concurrency hardening (per-note locks, path-scoped commits) and error masking.

## v0.2.0 - removed

Initial release: local stdio + HTTP server, 14 git/Obsidian-aware tools, per-vault ACL,
path guards. This tag was deleted; use v0.3.0 or later.
