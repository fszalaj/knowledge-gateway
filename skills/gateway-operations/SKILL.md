---
name: gateway-operations
description: Operate and release knowledge-gateway safely using its stable-branch distribution model, immutable version tags, PyPI Trusted Publishing, updater units, and health checks. Use for server updates, release preparation, publishing, rollback, or post-release verification.
---

# Operate and release Knowledge Gateway

Separate an ordinary code change from a release. Do not move `stable`, create a version tag, publish a package, or restart a server unless that action is explicitly in scope.

## Operate a server

1. Confirm the configured vault and token files, service identity, current executable version, and current `stable` revision without printing secrets.
2. Inspect `deploy/knowledge-gateway.service`, `deploy/knowledge-gateway-update.service`, `deploy/knowledge-gateway-update.timer`, and `deploy/auto-update.sh` before changing host state.
3. For an immediate update, reinstall from `@stable`, restart the exact service, and verify that it stays active.
4. Probe `http://127.0.0.1:8765/mcp/`. An unauthenticated HTTP `401` confirms the authenticated endpoint is reachable; use an authorized MCP smoke test to verify `list_vaults` and one bounded `read_note`.
5. Record the deployed revision. If validation fails, restore the previously known version or tag and re-run the same checks.

The bundled updater compares the remote `stable` SHA, reinstalls, restarts, and records the new SHA only after those steps succeed.

## Prepare a release

1. Start from current `main` on a release branch.
2. Move the `Unreleased` changelog entries into the new version section and update the version in `pyproject.toml` and `server.json`.
3. Run `uv lock --check`, the skill validator, the full test suite, and `uv build`. Inspect the built metadata and complete diff.
4. Open a pull request and require green CI for Python 3.11, 3.12, and 3.13. Merge only the reviewed release commit.
5. Tag the exact green `main` commit as `vX.Y.Z` and push the immutable tag. The tag workflow builds artifacts, creates the GitHub release, and publishes to PyPI through OIDC Trusted Publishing.
6. Verify the GitHub release and PyPI publication before moving `stable` to the same tag with `--force-with-lease`.
7. Verify one refreshed `uvx --refresh @stable` client and each managed server health check.

## Completion report

Return the commit, tag, `stable` SHA, CI and release workflow results, PyPI version, server health, rollback point, and any consumer action still required.
