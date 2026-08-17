---
name: gateway-setup
description: Configure knowledge-gateway for a repository, select the required optional capabilities, preserve existing MCP servers, and verify the connection. Use when onboarding a repo, enabling graph or document workflows, or troubleshooting missing tools.
---

# Knowledge Gateway setup

Configure the gateway without replacing existing MCP servers or committing secrets.

## Choose the capability set

- Core vault only: no extra.
- Python and Ansible graph: `[graph]`.
- Broad tree-sitter graph: `[graph-all]`.
- Document conversion: `[convert]`.
- Complete skill pack: `[all]`.

For the full local skill pack, merge this project-level entry into `.mcp.json`:

```jsonc
{
  "mcpServers": {
    "wiki": {
      "command": "uvx",
      "args": [
        "--refresh",
        "--from",
        "knowledge-gateway[all]",
        "knowledge-gateway",
        "--local"
      ]
    }
  }
}
```

If auto-detection is ambiguous, replace `--local` with `--vault ./<vault-directory>`.

## Procedure

1. Inspect the existing `.mcp.json`. Merge the `wiki` entry into `mcpServers`; never discard other servers.
2. Confirm the intended vault. Local discovery checks the current directory when it contains `.obsidian/`, then `./wiki`, one `*-obsidian-vault/` directory, or one child containing `.obsidian/`.
3. Start a fresh agent session and call `list_vaults`.
4. Call `read_note` for one known page and `git_status` to verify the vault and repository scope.
5. For graph workflows, call `list_graphs`, then make one successful bounded `graph_query` against an existing graph. If none exists, use `code-graph-build` locally before querying it.
6. For document ingestion, make one successful `convert_to_markdown` call on a small supported file already inside the vault. Tool registration alone does not prove that the optional converter dependencies are installed.

## Shared mode

Use shared HTTP mode only behind HTTPS or a trusted tailnet. It requires a per-user bearer token and per-vault ACL. Never write a token into the repository, a skill, an issue, or a chat transcript.

## Completion gate

Do not claim setup is complete until `list_vaults`, one `read_note`, and `git_status` succeed. For an optional capability, require a successful bounded operation rather than tool presence.
