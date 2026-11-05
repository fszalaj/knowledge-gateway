# knowledge-gateway

<!-- mcp-name: io.github.fszalaj/knowledge-gateway -->

knowledge-gateway is one MCP server that lets coding agents (Claude Code, Codex, Cursor,
Copilot, Gemini) read, search and edit a git-backed Markdown or Obsidian vault, and query a code
graph of your repositories, over a single connection. It exists because the usual route, the
Obsidian Local REST API plugin, serves only the vault open in a running desktop app, writes
without a lock, needs a token in every client and treats git as an afterthought. The gateway
works on the files directly, takes a lock for every edit, and makes git the system of record.

## What you get

- **Vault.** Read, search and edit notes, frontmatter and Obsidian Canvas files, and read
  attachments, without the Obsidian app. Edits are atomic and can commit straight away, scoped to the vault
  and attributed to whoever made them.
- **Code graph.** A static map of a repository: definitions, imports and calls in about 30
  languages, plus how Ansible, Docker, Kubernetes, CI pipelines and similar files point at each
  other. Built locally from syntax, with no network and no LLM.
- **Convert.** Turn PDF, Office, image and HTML files in the vault into Markdown.

Install `knowledge-gateway[graph]` for the vault and the code graph. Conversion is the separate
`[convert]` extra because its PDF and Office parsers pull in a much larger dependency tree;
`[all]` gives you everything.

## Architecture

```mermaid
flowchart LR
    subgraph clients [Agents]
        A1[Claude Code]
        A2[Codex]
        A3[Copilot - VS Code]
        A4[Cursor]
        A5[Antigravity - Gemini]
    end
    A1 --- M(( MCP ))
    A2 --- M
    A3 --- M
    A4 --- M
    A5 --- M
    M -->|stdio, per repo, no auth| L[Local gateway]
    M -->|HTTP + bearer + ACL| S[Shared gateway]
    L --> V[/Vault: Markdown files/]
    S --> V
    V <-->|atomic write + scoped commit| G[(git)]
```

Both modes run the same tools over the same path guards; they differ in transport, authentication and
access control, vault loading, and error masking.

## Quickstart (local mode)

Local mode runs the gateway as a subprocess of your agent, for the repository you have open. It
needs no tokens and no server. Add this to `.mcp.json` at the repository root:

```jsonc
{
  "mcpServers": {
    "wiki": {
      "command": "uvx",
      "args": ["--refresh", "--from", "knowledge-gateway[graph]",
               "knowledge-gateway", "--local"]
    }
  }
}
```

Open the repository in your agent, approve the `wiki` server once, and you are done.

`--local` finds the vault in the working directory: the directory itself if it has `.obsidian/`,
then `./wiki`, then a single `*-obsidian-vault/` directory, then a single child directory with
`.obsidian/`. If more than one matches it stops with an error; pass `--vault ./<dir>` instead.
`--refresh` picks up the latest release on every launch. Commits use your own
`git config user.name` and `user.email`.

Use `knowledge-gateway[all]` in the snippet if you also want conversion.

**Claude Code plugin.** This repository is also a Claude Code plugin that bundles the agent
skills. From a clone, run `claude --plugin-dir .` and the skills appear as
`/knowledge-gateway:<skill>`. See [docs/agent-skills.md](docs/agent-skills.md) for a persistent
install and its caveats.

## Tools

| Purpose | Tools |
|---|---|
| Find and read notes | `list_vaults`, `list_notes`, `read_note`, `search` (ripgrep), `backlinks`, `list_tags`, `query_notes` (by frontmatter `type` or tag) |
| Edit notes | `write_note`, `patch_note` (insert under a heading or at the top or bottom), `patch_frontmatter`, `delete_note`, `rename_note` (rewrites inbound wikilinks) |
| Git | `git_status`, `git_commit` |
| Attachments and canvases | `list_attachments`, `read_attachment`, `list_canvases`, `read_canvas`, `write_canvas` |
| Code graph | `list_graphs`, `graph_query`, `graph_neighbors`, `god_nodes`, `graph_shortest_path`, `graph_stats`, and `graph_build` (local mode only) |
| Convert | `convert_to_markdown` (needs `[convert]`; allowlisted document types only) |

Every edit writes to a temporary file and renames it into place, so a reader never sees half a
note. Each tool checks that the path it touches stays inside the vault.

## Code graph

The graph answers questions like "what calls this", "what breaks if I change this role" or "which
pipeline deploys this project". It is built from syntax only: nothing is executed, rendered or
fetched. Besides ordinary code, dedicated passes read the files a language parser cannot turn
into structure:

| Technology | What the graph shows |
|---|---|
| Code (Python and about 30 tree-sitter languages) | definitions, imports, calls |
| Ansible | roles, tasks, playbooks, inventories, which hosts a play targets |
| Docker, Kubernetes, Helm, Bicep | images and stages, resources, charts and values, modules and deployments |
| CI (GitHub Actions, Jenkins) | workflows, jobs and stages, the scripts they run, what they build and deploy |
| Cloud and data platforms (Microsoft Fabric, Elastic Beanstalk) | artifacts, pipelines and environments, and the code behind them |
| Apps and APIs (Appsmith, HTTP files, ASP.NET Core) | pages, queries and requests, endpoints, and which client calls which endpoint |
| Dependency manifests | packages and project references |

The dedicated passes store names and references, not values: variable, secret, header and
body values are left out. Import specifiers are kept as written, so a URL import keeps its query
string; the graph is not a secret scrubber.

Build one where the code lives (in a local-mode session the `graph_build` tool does the same):

```bash
knowledge-gateway-graph /path/to/repo -o /path/to/vault/.graph/myrepo.json
```

Install profiles, every pass, the metadata the graph reports and its known limits are in
[docs/code-graph.md](docs/code-graph.md).

## Agent skills

The repository ships a pack of agent skills in `.agents/skills/`: setup and operations, building
and exploring the code graph, impact analysis, curating and querying the wiki, Canvas and
Obsidian authoring, and document conversion. Codex, Gemini CLI, GitHub Copilot and Cursor find
that directory on their own; Claude Code loads it through the plugin described above. Skills add
procedures, not privileges: access control stays in the server.

To use skills in another repository, copy the directories you need into that repository and
review them there. [docs/agent-skills.md](docs/agent-skills.md) has the client compatibility
table, copy instructions and maintenance rules.

## Shared server mode

For a team, run one always-on gateway over HTTP, with a bearer token per person and a per-vault
access list.

1. Copy `vaults.example.yaml` to `vaults.yaml` and map each vault name to its path. `repo_root`
   and `subdir` scope commits when a vault lives inside a larger repository.
2. Copy `tokens.example.yaml` to `tokens.yaml` and add one token per user (generate each with
   `openssl rand -hex 32`), then `chmod 0600 tokens.yaml`. The gateway refuses a tokens file that
   other users can read.

   ```yaml
   tokens:
     "<64 hex characters>":
       sub: alice              # identity recorded on alice's commits
       vaults: [teamwiki]      # the only vaults this token can see
       write: true             # false means read-only
   ```

3. Start it with `uv run knowledge-gateway` (listens on `127.0.0.1:8765`, path `/mcp/`). For a
   permanent install, see [docs/operations.md](docs/operations.md).
4. Share each token through a password manager, never chat. Users connect with:

   ```bash
   claude mcp add --transport http --scope project teamwiki \
     https://gateway.example.com/mcp/ --header "Authorization: Bearer $GW_TOKEN"
   ```

Both modes run the same tools behind the same path checks; they differ only in transport,
authentication and how much error detail reaches the client.

## Security model

- No secrets in the repository: `vaults.yaml` and `tokens.yaml` are gitignored and only the
  `*.example.yaml` files ship.
- Local mode has no credentials at all. Its trust boundary is the filesystem access you already
  have.
- Shared mode checks a bearer token on every request, and a token sees only the vaults it lists.
  Any other vault returns the same opaque `vault_forbidden`, whether it exists or not.
- Note paths are checked against traversal, symlink escape, hidden files (including `.env`),
  `.git`, `.obsidian` and non-Markdown files. Attachments, canvases and graph files have their
  own containment checks.
- Shared mode masks unexpected errors, so OS or git details never reach a client. Local mode
  shows them.
- Commits are scoped to the vault's directory and attributed to the requesting user (shared) or
  your git identity (local).

## Release

```mermaid
flowchart LR
    PR[merge version bump PR to main] --> CI[green ci on main]
    CI --> REL["release.yml<br/>publish PyPI + tag vX.Y.Z"]
    REL --> C["Consumers<br/>uvx --refresh knowledge-gateway<br/>(updates next session)"]
    REL --> S["Servers<br/>daily uv tool reinstall<br/>(restart if the version moved)"]
```

Releases go to PyPI. Consumers using `uvx --refresh` get a new release in their next session, and
servers within a day. A release is cut by bumping the version on `main`; the workflow publishes
and tags it once CI is green. Every release is also an immutable `vX.Y.Z` tag for pinning.
The procedure, branch policy and server operations are in [docs/operations.md](docs/operations.md),
together with a prompt that lets an agent set up local mode for you.

## Develop

```bash
uv venv && uv pip install -e ".[dev]"
uv run python scripts/validate-skills.py
uv run pytest -q
```

Engineering rules for contributors, human or agent, are in [AGENTS.md](AGENTS.md). User-visible
changes go in [CHANGELOG.md](CHANGELOG.md).
