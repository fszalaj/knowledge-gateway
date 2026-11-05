# Operations

How the gateway is distributed, how servers stay current, and how a release is cut. The README
covers setup; this page is for people who run servers or publish releases.

## Distribution

The gateway ships as a PyPI package, so a release reaches every consumer and server without
re-pinning anything by hand.

- **Consumers** run `uvx --refresh --from 'knowledge-gateway[graph]' knowledge-gateway --local`.
  `--refresh` resolves the latest release on every launch, so a new release applies the next
  time an agent starts (it adds a second or two to startup).
- **Servers** install with `uv tool install 'knowledge-gateway[graph,convert]'` and run a daily
  job that reinstalls and restarts only when the published version moves.
- Every release is also an immutable `vX.Y.Z` git tag. Pin `==X.Y.Z` when you need a frozen,
  auditable version.

## Branches and tags

- `main` is development and the latest release. An automatic release is cut only from a green
  `ci` run there; a manually pushed tag skips that gate (see [Releasing](#releasing)).
- `vX.Y.Z` is an immutable tag per release.

The `stable` branch was retired in v0.12.0 and must not be recreated. Migrate an `@stable`
requirement to PyPI and keep its extras and server arguments, for example
`uvx --refresh --from 'knowledge-gateway[graph]' knowledge-gateway --local`. Running `main` from
git does not track published releases.

## Operating a server

A server runs the latest release as a `uv tool`. Reference systemd units are in `deploy/`:

- `deploy/knowledge-gateway.service` runs the gateway (systemd `--user`).
- `deploy/knowledge-gateway-update.service`, `deploy/knowledge-gateway-update.timer` and
  `deploy/auto-update.sh` run the daily update.

```bash
uv tool install 'knowledge-gateway[graph,convert]'
# The binary lives in the uv tool directory, so point it at the live config files:
#   KNOWLEDGE_GATEWAY_VAULTS=<dir>/vaults.yaml  KNOWLEDGE_GATEWAY_TOKENS=<dir>/tokens.yaml
```

The server listens on `127.0.0.1:8765` at `/mcp/` by default; `KNOWLEDGE_GATEWAY_HOST`,
`KNOWLEDGE_GATEWAY_PORT` and `KNOWLEDGE_GATEWAY_PATH` change that. To update without waiting for
the timer, run `uv tool install --reinstall 'knowledge-gateway[graph,convert]'` and restart the
service. A health check without a token should return `401`:

```bash
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/mcp/
```

## Releasing

A release is a version bump and nothing else. `release.yml` watches for a green `ci` run on
`main` whose `pyproject.toml` version has no tag yet. It then builds, publishes to PyPI through
Trusted Publishing (no token), and creates the `vX.Y.Z` GitHub Release and tag. No branch is
created or moved. Code merged without a version bump stays unreleased.

1. Move the `Unreleased` changelog entries into a new version section, and bump the version in
   `pyproject.toml`, `server.json` and `.claude-plugin/plugin.json`.
2. Run `uv lock` so the lockfile carries the new version, then the skill validator and the test
   suite. Open a pull request and merge only after the Python 3.11-3.13 CI matrix is green.
3. Watch the `release` run. Check the source commit, the tag, and that the wheel and sdist
   digests match on PyPI and on the GitHub Release.

The tag is created after PyPI publishing. GitHub assets are the verified bytes downloaded back
from PyPI, with their runtime sources checked against the release tree. An incomplete tagged
release resumes from that tag, and published assets with conflicting digests are refused. The
workflow can succeed without publishing when the tag, the GitHub Release and matching PyPI
artifacts already exist.

If PyPI has the version but the tag is missing, automatic publication stops. Inspect the
original workflow run and its source evidence before recreating the tag and retrying. A later
commit must never claim a version that is already published. Pushing a `vX.Y.Z` tag by hand is
still supported, and the workflow checks that it matches the packaged version, but it starts
publication straight away without waiting for CI on the tagged commit, so push a tag only on a
commit whose `ci` run is already green.

Consumers pick up a release in their next session; servers within a day, or immediately after a
manual reinstall.

## Set it up with an AI

Paste this into an agent at the root of a repository to wire in local mode:

```
Add the knowledge-gateway to this repo so agents can read and edit our vault over MCP with no
tokens:
1. Create or merge `.mcp.json` at the repo root with an mcpServers."wiki" entry that runs:
   uvx --refresh --from 'knowledge-gateway[graph]' knowledge-gateway --local
   (`--local` auto-detects the vault: ./wiki, a *-obsidian-vault dir, or a dir with .obsidian/.
   If detection is ambiguous, use `--vault ./<vault dir>` instead of `--local`.)
2. Verify: `uvx --refresh --from 'knowledge-gateway[graph]' knowledge-gateway --help` resolves;
   then, in the agent, call list_vaults and read one note.
Work on a branch and open a PR. No direct push, no AI attribution.
```

For a shared server, ask the gateway admin for a token and use the `claude mcp add` command from
the README.
