# Changelog

All notable changes to obsidian-gateway. Tags are immutable releases - pin a commit SHA
(or, once published, a PyPI version) in consumers; do not rely on a moving tag.

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
- PyPI Trusted Publishing, so consumers can `uvx obsidian-gateway==<version>` without a git fetch.
- Server-mode concurrency hardening (per-note locks, path-scoped commits) and error masking.

## v0.2.0 - removed

Initial release: local stdio + HTTP server, 14 git/Obsidian-aware tools, per-vault ACL,
path guards. This tag was deleted; use v0.3.0 or later.
