---
name: gateway-operations
description: Operate and release knowledge-gateway safely using its stable-branch distribution model, immutable version tags, PyPI Trusted Publishing, updater units, and health checks. Use for server updates, release preparation, publishing, rollback, or post-release verification.
---

# Operate and release Knowledge Gateway

Separate an ordinary code change from a release. Do not move `stable`, create a version tag, publish a package, or restart a server unless that action is explicitly in scope.

## Operate a server

1. Confirm the configured vault and token files, service identity, current executable version, and current `stable` revision without printing secrets.
2. Inspect `deploy/knowledge-gateway.service`, `deploy/knowledge-gateway-update.service`, `deploy/knowledge-gateway-update.timer`, and `deploy/auto-update.sh` before changing host state.
3. For an immediate update, reinstall the latest PyPI release, restart the exact service, and verify that it stays active.
4. Probe `http://127.0.0.1:8765/mcp/`. An unauthenticated HTTP `401` confirms the authenticated endpoint is reachable; use an authorized MCP smoke test to verify `list_vaults` and one bounded `read_note`.
5. Record the deployed revision. If validation fails, restore the previously known version or tag and re-run the same checks.

The bundled updater (`deploy/auto-update.sh`) compares the latest PyPI version with the installed one and reinstalls plus restarts only when they differ. It keeps no marker file, so the installed version is the state and a half-finished update heals on the next run.

## Prepare a release

1. Start from current `main` on a release branch.
2. Move the `Unreleased` changelog entries into the new version section and update the version in `pyproject.toml` and `server.json`.
3. Run `uv lock --check`, the skill validator, the full test suite, and `uv build`. Inspect the built metadata and complete diff.
4. Open a pull request and require green CI for Python 3.11, 3.12, and 3.13. Merge only the reviewed release commit.
5. Merge the reviewed version bump. `release.yml` releases from the green `ci` run on `main`: it builds, publishes to PyPI through OIDC Trusted Publishing, creates the `vX.Y.Z` GitHub release (which creates the tag), and fast-forwards `stable`. Do not tag or move `stable` by hand; a tag pushed before a successful upload burns that version.
6. Watch the release run to completion, then verify the GitHub release, the PyPI version and that `stable` points at the tag.
7. Verify one refreshed `uvx --refresh` client and each managed server health check.

## Completion report

Return the commit, tag, `stable` SHA, CI and release workflow results, PyPI version, server health, rollback point, and any consumer action still required.
