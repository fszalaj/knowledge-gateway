from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from fastmcp import FastMCP

try:  # the StaticTokenVerifier import path has shifted across FastMCP releases
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
except ImportError:  # pragma: no cover
    from fastmcp.server.auth import StaticTokenVerifier  # type: ignore

from . import acl, config, gitops
from .tools import register_tools
from .vaults import Vault

INSTRUCTIONS = (
    "Read, search, and edit Markdown notes in a git-backed Obsidian vault - no Obsidian app "
    "required. Discover with list_vaults / list_notes; read with read_note; find with search "
    "(ripgrep), backlinks, list_tags, and query_notes (by frontmatter type/tag). Read binary "
    "attachments (images, PDF, audio, video) with list_attachments / read_attachment, and "
    "Obsidian Canvas files (nodes, groups, colors) with list_canvases / read_canvas / write_canvas. "
    "Edit notes with write_note, patch_note (insert after a heading or at top/bottom), patch_frontmatter (YAML "
    "keys only), delete_note, and rename_note; review and commit pending changes with git_status "
    "and git_commit. Conventions: note paths are relative to the vault and end in .md; wikilinks "
    "are [[Note Name]] by flat filename; prefer patch_* over rewriting a whole note. If the vault "
    "has a _templates/ folder, read the matching _templates/<type>.md before creating a page. Edits are "
    "atomic, and each edit can optionally commit (commits are pathspec-scoped to the vault and "
    "attributed to the caller); git is the source of truth."
)


def build_server() -> FastMCP:
    vaults = config.load_vaults()
    registry = acl.build_registry(config.load_tokens())
    token_map = {
        token: {"client_id": info.sub, "scopes": acl.scopes_for(info)}
        for token, info in registry.items()
    }
    authors = {info.sub: info.email for info in registry.values()}
    mcp = FastMCP("obsidian-gateway", instructions=INSTRUCTIONS, auth=StaticTokenVerifier(tokens=token_map), mask_error_details=True)
    register_tools(mcp, vaults, authors)
    return mcp


def build_local_server(vault_path: str) -> FastMCP:
    """A single-vault, no-auth server for local stdio launch from a repo's .mcp.json.
    The trust boundary is local filesystem access, so there are no tokens. Git
    commits use the machine's own git identity. repo_root is auto-detected so
    commits stay scoped to the vault subdir within a larger repo."""
    p = Path(vault_path).expanduser().resolve()
    root = p
    try:
        cand = Path(gitops._git(p, "rev-parse", "--show-toplevel").strip()).resolve()
        # Only adopt the git root if p is genuinely inside it. A case-insensitive
        # FS or symlink can make git's toplevel differ from p.resolve(); if it is
        # not a clean parent, fall back to treating the vault as its own root
        # (git still scopes commits to the vault dir) instead of crashing.
        if p == cand or p.is_relative_to(cand):
            root = cand
    except Exception:
        pass
    subdir = "." if root == p else p.relative_to(root).as_posix()
    name = p.name
    vault = Vault(name=name, path=p, repo_root=root, subdir=subdir, description=f"local vault: {name}")
    mcp = FastMCP("obsidian-gateway", instructions=INSTRUCTIONS, mask_error_details=False)
    register_tools(mcp, {name: vault}, authors=None, local=True)
    return mcp


def main() -> None:
    ap = argparse.ArgumentParser(prog="obsidian-gateway")
    ap.add_argument("--vault", help="serve THIS single vault locally over stdio (no auth)")
    ap.add_argument("--local", action="store_true", help="auto-detect the cwd's vault and serve it locally over stdio")
    args, _ = ap.parse_known_args()

    vault = args.vault or os.environ.get("OBSIDIAN_GATEWAY_VAULT")
    if not vault and (args.local or os.environ.get("OBSIDIAN_GATEWAY_LOCAL", "").strip().lower() in {"1", "true", "yes", "on"}):
        from .detect import VaultDetectionError, detect_vault
        try:
            vault = str(detect_vault(Path.cwd()))
        except VaultDetectionError as e:
            print(f"obsidian-gateway: {e}", file=sys.stderr)
            raise SystemExit(2)
    if vault:
        build_local_server(vault).run(transport="stdio")
        return

    host = os.environ.get("OBSIDIAN_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("OBSIDIAN_GATEWAY_PORT", "8765"))
    path = os.environ.get("OBSIDIAN_GATEWAY_PATH", "/mcp/")
    build_server().run(transport="http", host=host, port=port, path=path)


if __name__ == "__main__":
    main()
