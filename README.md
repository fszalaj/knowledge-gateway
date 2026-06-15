# obsidian-gateway

A filesystem/git-native **MCP gateway** for Obsidian vaults. It lets AI agents
(Claude Code, Codex, Cursor, …) read, search and *edit* a vault with git-aware,
Obsidian-aware operations - **no Obsidian GUI has to be running**, and git stays
the single source of truth.

It exists because the Obsidian *Local REST API* plugin serves only the one vault
open in a running desktop instance, writes without a lock (silent lost updates),
needs a token pasted into every client, and fights git-as-source-of-truth. This
gateway talks to the markdown files directly.

---

## Two ways to run it

| | **Local mode** (per repo) | **Shared server** (team) |
|---|---|---|
| Use when | a repo wants its own vault for its agents | many people/vaults behind one always-on endpoint |
| Transport | stdio subprocess (launched by `.mcp.json`) | HTTPS (e.g. over Tailscale) |
| **Secrets / tokens** | **none** - nothing to generate | per-user bearer tokens (admin-generated) |
| Trust boundary | local filesystem access you already have | tailnet + HTTPS + per-user ACL |
| Obsidian needed | no | no |

Most repos want **Local mode**. The shared server is only for a central,
always-on team gateway over the network.

---

## Local mode - zero secrets, zero setup

Add this to the repo's `.mcp.json` (at the repo root). Every contributor's agent
then gets the gateway automatically - **no token to generate, nothing to paste**:

```jsonc
{
  "mcpServers": {
    "wiki": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/fszalaj/obsidian-gateway@v0.2.0",
               "obsidian-gateway", "--vault", "./wiki"]
    }
  }
}
```

- `uvx` fetches + runs the gateway in an ephemeral env (first launch builds, then
  cached). Pin a tag (`@v0.2.0`) for reproducibility.
- It auto-detects the git repo root, so commits stay scoped to the vault subdir
  and are attributed to your own `git config user.name/email`.
- **Who can use it:** anyone who can clone the repo. The trust boundary is local
  filesystem access - there is no token because there is no network surface.

That is the whole setup. Open the repo in your agent, approve the `wiki` server
once, done.

---

## What you can do (tools)

| Tool | |
|---|---|
| `list_vaults` | vaults available here |
| `list_notes` | markdown paths in a vault |
| `read_note` | raw note content |
| `search` | ripgrep literal/regex full-text |
| `backlinks` | notes that `[[wikilink]]` to a note |
| `list_tags` | inline `#tags` with counts |
| `query_notes` | find notes by frontmatter `type` / `tag` (headless Dataview-lite) |
| `write_note` | atomic write (+ optional commit) |
| `patch_note` | insert without rewriting: after a heading, or top/bottom (+ commit) |
| `patch_frontmatter` | update YAML frontmatter keys, body/comments intact (+ commit) |
| `delete_note` | delete a note (+ optional commit) |
| `git_status` / `git_commit` | pending changes / commit (subdir-scoped, attributed) |

Edits are atomic (temp file + rename); every path goes through guards that block
traversal, hidden files, non-`.md` targets and `.git`/`.obsidian` - so a caller
can never read or write outside the vault's notes.

---

## Shared server mode - one gateway, many vaults, many users

Run this only if you want a central, always-on gateway reachable over the network.

### 1. Map your vaults

```bash
cp vaults.example.yaml vaults.yaml      # edit: name -> path / repo_root / subdir
```

`repo_root` + `subdir` matter when a vault is a subdirectory of a bigger repo:
commits are pathspec-scoped to the subdir so they never sweep in sibling code.

### 2. Generate a token per user  (who generates secrets, and how)

The **person running the server (the admin)** mints one bearer token per user:

```bash
cp tokens.example.yaml tokens.yaml
openssl rand -hex 32                     # run once PER user -> paste as the key
chmod 0600 tokens.yaml
```

```yaml
# tokens.yaml  (gitignored - never committed)
tokens:
  "8f3c…the-hex-token…":          # the value from `openssl rand -hex 32`
    sub: alice                    # identity recorded on that user's commits
    vaults: [teamwiki]            # the ONLY vaults this token may see/touch
    write: true                   # false = read-only
```

A token can only see, enumerate and reach the vaults in its `vaults` list -
everything else returns an opaque `vault_forbidden`, indistinguishable from a
vault that does not exist. `vaults.yaml` + `tokens.yaml` are gitignored: paths
and secrets live only on the box that runs the gateway.

### 3. Run / deploy

```bash
uv run obsidian-gateway                  # 127.0.0.1:8765, path /mcp/
```

For a team box, put it behind Tailscale Serve (private HTTPS, tailnet-only) and
run it as a service - see `deploy/tailscale.md` and `deploy/obsidian-gateway.service`.

### 4. Connect a client  (how a user joins)

The admin shares that user's token over a secure channel (password manager, not
chat). The user adds the server once:

```bash
export GW_TOKEN=…their-token…
claude mcp add --transport http --scope project teamwiki \
  https://YOUR-GATEWAY-HOST.<tailnet>.ts.net/mcp/ \
  --header "Authorization: Bearer $GW_TOKEN"
```

---

## Security model

- **No secrets in the repo.** `vaults.yaml` and `tokens.yaml` are gitignored;
  only `*.example.yaml` (placeholders) ship. The code contains no credentials.
- **Local mode has no secret surface** - it is a local stdio subprocess; the
  trust boundary is filesystem access the user already has.
- **Server mode: three independent layers** - tailnet ACL (network) + HTTPS
  (transport) + per-user bearer/ACL (application).
- **Path guards everywhere** - traversal, symlink-escape, hidden files, non-`.md`
  and `.git`/`.obsidian`/`.env` are rejected on every read/write/delete.
- **Commits are attributed** to the requesting user (server) or the local git
  identity (local), and pathspec-scoped to the vault subdir.

---

## Set it up with an AI

Paste this into an agent at a repo's root to wire in local mode:

```
Add the obsidian-gateway to this repo so agents can read/edit our vault over MCP,
with zero tokens. Steps:
1. Find the vault dir (the folder with the markdown / an .obsidian/ folder; often
   `wiki/` or `<repo>-obsidian-vault/`).
2. Create/merge `.mcp.json` at the repo root with an mcpServers."wiki" entry that
   runs: uvx --from git+https://github.com/fszalaj/obsidian-gateway@v0.2.0
   obsidian-gateway --vault ./<that vault dir>.
3. Verify: `uvx --from git+https://github.com/fszalaj/obsidian-gateway@v0.2.0 \
   obsidian-gateway --help` resolves; then in the agent, call list_vaults and
   read_note on one note to confirm it connects.
Branch + PR, no direct push, no AI attribution.
```

For the shared server, ask your gateway admin for a token, then run the
`claude mcp add … --header "Authorization: Bearer …"` from "Connect a client".

---

## Develop

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest                            # ACL + path guards + edit/frontmatter logic
```
