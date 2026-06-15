from __future__ import annotations

import argparse
import os
from pathlib import Path

from fastmcp import FastMCP

try:  # the StaticTokenVerifier import path has shifted across FastMCP releases
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
except ImportError:  # pragma: no cover
    from fastmcp.server.auth import StaticTokenVerifier  # type: ignore

from . import acl, config, gitops
from .tools import register_tools
from .vaults import Vault


def build_server() -> FastMCP:
    vaults = config.load_vaults()
    registry = acl.build_registry(config.load_tokens())
    token_map = {
        token: {"client_id": info.sub, "scopes": acl.scopes_for(info)}
        for token, info in registry.items()
    }
    authors = {info.sub: info.email for info in registry.values()}
    mcp = FastMCP("obsidian-gateway", auth=StaticTokenVerifier(tokens=token_map))
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
    mcp = FastMCP("obsidian-gateway")
    register_tools(mcp, {name: vault}, authors=None, local=True)
    return mcp


def main() -> None:
    ap = argparse.ArgumentParser(prog="obsidian-gateway")
    ap.add_argument("--vault", help="serve THIS single vault locally over stdio (no auth)")
    args, _ = ap.parse_known_args()

    vault = args.vault or os.environ.get("OBSIDIAN_GATEWAY_VAULT")
    if vault:
        build_local_server(vault).run(transport="stdio")
        return

    host = os.environ.get("OBSIDIAN_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("OBSIDIAN_GATEWAY_PORT", "8765"))
    path = os.environ.get("OBSIDIAN_GATEWAY_PATH", "/mcp/")
    build_server().run(transport="http", host=host, port=port, path=path)


if __name__ == "__main__":
    main()
