"""Shared HTTP mode, end to end through the tool layer.

Every other tool test builds a LOCAL server, where `authorize()` is deliberately skipped
because the trust boundary is filesystem access. So the per-vault ACL, the write scope and
the commit attribution that only exist when `register_tools(..., local=False)` runs had no
test that a dropped check could fail.
"""
import subprocess
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from gateway import acl, tools
from gateway.vaults import Vault

TOKENS = {
    "tok_team": {"sub": "bob", "vaults": ["teamwiki"], "write": True, "email": "bob@example.com"},
    "tok_ro": {"sub": "ci", "vaults": ["teamwiki"], "write": False},
}


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout


def _vault(root, name):
    path = root / name
    path.mkdir()
    _git(root, "init", "-b", "main", str(path))
    _git(path, "config", "user.email", "svc@host")
    _git(path, "config", "user.name", "Service")
    (path / "a.md").write_text("# A\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "seed")
    return Vault(name=name, path=path, repo_root=path, subdir=".")


@pytest.fixture
def shared(tmp_path):
    vaults = {n: _vault(tmp_path, n) for n in ("teamwiki", "personal")}
    registry = acl.build_registry(TOKENS)

    def build(token):
        info = registry[token]
        tools.get_access_token = lambda: SimpleNamespace(  # patched per server build
            scopes=acl.scopes_for(info), client_id=info.sub)
        mcp = FastMCP("shared", mask_error_details=True)
        tools.register_tools(mcp, vaults, authors={i.sub: i.email for i in registry.values()})
        return mcp

    original = tools.get_access_token
    yield vaults, build
    tools.get_access_token = original


async def test_token_cannot_reach_a_vault_it_was_not_granted(shared):
    _, build = shared
    async with Client(build("tok_team")) as c:
        assert [v["vault"] for v in (await c.call_tool("list_vaults", {})).data] == ["teamwiki"]
        with pytest.raises(ToolError, match="vault_forbidden"):
            await c.call_tool("read_note", {"vault": "personal", "path": "a.md"})
        with pytest.raises(ToolError, match="vault_forbidden"):   # same answer for a vault
            await c.call_tool("read_note", {"vault": "nope", "path": "a.md"})  # that does not exist


async def test_read_only_token_cannot_write(shared):
    vaults, build = shared
    async with Client(build("tok_ro")) as c:
        await c.call_tool("read_note", {"vault": "teamwiki", "path": "a.md"})
        with pytest.raises(ToolError, match="write_forbidden"):
            await c.call_tool("write_note", {"vault": "teamwiki", "path": "b.md", "content": "x"})
    assert not (vaults["teamwiki"].path / "b.md").exists()


async def test_commit_is_attributed_to_the_requesting_token(shared):
    vaults, build = shared
    async with Client(build("tok_team")) as c:
        r = await c.call_tool("write_note", {"vault": "teamwiki", "path": "b.md",
                                             "content": "x\n", "commit": True})
    assert r.data["sub"] == "bob"
    repo = vaults["teamwiki"].path
    assert _git(repo, "log", "-1", "--format=%an <%ae>").strip() == "bob <bob@example.com>"
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["b.md"]
