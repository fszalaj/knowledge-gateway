from __future__ import annotations

import os

from fastmcp.server.dependencies import get_access_token

from . import acl, edits, gitops, links
from . import tags as tagmod
from .search import ripgrep
from .vaults import Vault
from .writes import atomic_write

MAX_NOTE_BYTES = 10 * 1024 * 1024  # read_note guard against a pathological huge file


def _scopes() -> list[str]:
    tok = get_access_token()
    return list(tok.scopes) if tok and tok.scopes else []


def _sub() -> str:
    tok = get_access_token()
    return getattr(tok, "client_id", None) or "unknown"


def register_tools(mcp, vaults: dict[str, Vault], authors: dict | None = None, local: bool = False) -> None:
    authors = authors or {}

    def _author() -> tuple[str, str]:
        if local:
            # Local stdio mode has no token; attribute commits to the machine's
            # own git identity, read from the single vault's repo.
            v = next(iter(vaults.values()))
            try:
                name = gitops._git(v.repo_root, "config", "user.name").strip()
                email = gitops._git(v.repo_root, "config", "user.email").strip()
            except Exception:
                name, email = "", ""
            return name, email
        # Attribute the git commit to the requesting user (the token's sub), not
        # the service account the gateway runs as. Committer stays the service.
        sub = _sub()
        return sub, authors.get(sub) or f"{sub}@local"

    def _vault(name: str, *, write: bool) -> Vault:
        # Local stdio mode is single-tenant on the local fs - the trust boundary
        # is filesystem access, so there is no token/ACL to enforce.
        if not local:
            acl.authorize(_scopes(), name, write=write)
        v = vaults.get(name)
        if v is None:
            # Token granted a vault that no longer exists — stay opaque.
            raise acl.AccessDenied(f"vault_forbidden: {name}")
        return v

    @mcp.tool
    def list_vaults() -> list[dict]:
        """List the vaults available here, each with a short description."""
        allowed = set(vaults) if local else acl.allowed_vaults(_scopes())
        return [
            {"vault": n, "description": vaults[n].description}
            for n in sorted(vaults)
            if n in allowed
        ]

    @mcp.tool
    def list_notes(vault: str, subdir: str | None = None, limit: int = 1000) -> list[str]:
        """List markdown note paths (vault-relative) within a vault."""
        v = _vault(vault, write=False)
        return v.list_markdown(subdir=subdir, limit=max(1, min(limit, 5000)))

    @mcp.tool
    def read_note(vault: str, path: str) -> str:
        """Read one markdown note's raw content."""
        v = _vault(vault, write=False)
        target = v.safe_note_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"not_found: {path}")
        if target.stat().st_size > MAX_NOTE_BYTES:
            raise ValueError(f"too_large: {path} is over {MAX_NOTE_BYTES // (1024 * 1024)} MiB")
        return target.read_text(encoding="utf-8")

    @mcp.tool
    def search(vault: str, query: str, regex: bool = False, limit: int = 50) -> list[dict]:
        """Search a vault with ripgrep — literal by default, regex when regex=true."""
        v = _vault(vault, write=False)
        return ripgrep(v.path, query, regex=regex, limit=limit)

    @mcp.tool
    def backlinks(vault: str, note: str, limit: int = 200) -> list[dict]:
        """Find notes that [[wikilink]] to the given note."""
        v = _vault(vault, write=False)
        return links.backlinks(v.path, note, limit=limit)

    @mcp.tool
    def list_tags(vault: str) -> list[dict]:
        """List inline #tags in a vault with occurrence counts."""
        v = _vault(vault, write=False)
        return tagmod.list_tags(v.path)

    @mcp.tool
    def git_status(vault: str) -> dict:
        """Show uncommitted changes scoped to the vault's subdir."""
        v = _vault(vault, write=False)
        return gitops.status(v)

    @mcp.tool
    def write_note(
        vault: str,
        path: str,
        content: str,
        commit: bool = False,
        message: str | None = None,
    ) -> dict:
        """Atomically write a note; optionally git-commit the change."""
        v = _vault(vault, write=True)
        target = v.safe_note_path(path)
        atomic_write(target, content)
        result = {"vault": vault, "written": path, "sub": _sub()}
        if commit:
            result["commit"] = gitops.commit(v, message or f"update {path}", author=_author())
        return result

    @mcp.tool
    def git_commit(vault: str, message: str) -> dict:
        """Commit the vault's pending changes (scoped to its subdir)."""
        v = _vault(vault, write=True)
        return gitops.commit(v, message, author=_author())

    @mcp.tool
    def patch_note(
        vault: str,
        path: str,
        content: str,
        under_heading: str | None = None,
        position: str = "bottom",
        commit: bool = False,
        message: str | None = None,
    ) -> dict:
        """Insert content into a note without rewriting it: after a heading (under_heading),
        or at the top/bottom (position). Frontmatter is preserved. Optionally git-commit."""
        v = _vault(vault, write=True)
        target = v.safe_note_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"not_found: {path}")
        new = edits.insert_markdown(
            target.read_text(encoding="utf-8"), content, under_heading=under_heading, position=position
        )
        atomic_write(target, new)
        result = {"vault": vault, "patched": path}
        if commit:
            result["commit"] = gitops.commit(v, message or f"update {path}", author=_author())
        return result

    @mcp.tool
    def patch_frontmatter(
        vault: str,
        path: str,
        updates: dict,
        commit: bool = False,
        message: str | None = None,
    ) -> dict:
        """Update YAML frontmatter keys on a note (e.g. bump updated/status), leaving the
        body intact. Creates a frontmatter block if absent. Optionally git-commit."""
        v = _vault(vault, write=True)
        target = v.safe_note_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"not_found: {path}")
        new = edits.update_frontmatter(target.read_text(encoding="utf-8"), dict(updates))
        atomic_write(target, new)
        result = {"vault": vault, "frontmatter_updated": sorted(updates)}
        if commit:
            result["commit"] = gitops.commit(v, message or f"update frontmatter {path}", author=_author())
        return result

    @mcp.tool
    def delete_note(
        vault: str,
        path: str,
        commit: bool = False,
        message: str | None = None,
    ) -> dict:
        """Delete a note; optionally git-commit the deletion."""
        v = _vault(vault, write=True)
        target = v.safe_note_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"not_found: {path}")
        target.unlink()
        result = {"vault": vault, "deleted": path}
        if commit:
            result["commit"] = gitops.commit(v, message or f"delete {path}", author=_author())
        return result

    @mcp.tool
    def rename_note(
        vault: str,
        old_path: str,
        new_path: str,
        commit: bool = False,
        message: str | None = None,
    ) -> dict:
        """Rename/move a note AND update every [[wikilink]] to it across the vault
        (alias/heading/block/embed preserved). Requires flat-unique note names: refuses
        if the old name is ambiguous or the new name already exists. Path-qualified
        links ([[dir/Name]]) are not rewritten. Optionally commit."""
        v = _vault(vault, write=True)
        src = v.safe_note_path(old_path)
        dst = v.safe_note_path(new_path)
        if not src.is_file():
            raise FileNotFoundError(f"not_found: {old_path}")
        if dst.exists():
            raise FileExistsError(f"exists: {new_path}")
        old_stem = os.path.splitext(os.path.basename(old_path))[0]
        new_stem = os.path.splitext(os.path.basename(new_path))[0]

        planned: list[tuple] = []
        if old_stem != new_stem:
            all_notes = v.list_markdown(limit=None)
            stems: dict[str, int] = {}
            for rel in all_notes:
                stems[os.path.basename(rel)[:-3]] = stems.get(os.path.basename(rel)[:-3], 0) + 1
            if stems.get(old_stem, 0) > 1:
                raise ValueError(f"ambiguous_old_name: {stems[old_stem]} notes named '{old_stem}'")
            if stems.get(new_stem, 0) > 0:
                raise ValueError(f"new_name_taken: a note named '{new_stem}' already exists")
            # PASS 1: compute every rewrite in memory. A read error here raises BEFORE
            # the file is moved, so a failure leaves the vault untouched.
            for rel in all_notes:
                if rel.startswith("_templates/") or "/_templates/" in rel:
                    continue
                p = v.path / rel
                if p.is_symlink():
                    continue
                new_text, n = edits.rewrite_wikilinks(p.read_text(encoding="utf-8"), old_stem, new_stem)
                if n:
                    planned.append((p, new_text, n))

        # PASS 2: move, then apply the planned rewrites (the source's own links land in
        # the moved file at its new path).
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        touched: list[str] = []
        total = 0
        for p, new_text, n in planned:
            target = dst if p == src else p
            atomic_write(target, new_text)
            touched.append(target.relative_to(v.path).as_posix())
            total += n

        result = {"vault": vault, "renamed": f"{old_path} -> {new_path}", "links_updated": total, "files": touched}
        if commit:
            result["commit"] = gitops.commit(v, message or f"rename {old_path} -> {new_path}", author=_author())
        return result

    @mcp.tool
    def query_notes(
        vault: str,
        type: str | None = None,
        tag: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Find notes by frontmatter type and/or tag (headless Dataview-lite)."""
        v = _vault(vault, write=False)
        out: list[dict] = []
        for rel in v.list_markdown(limit=5000):
            data = edits.read_frontmatter((v.path / rel).read_text(encoding="utf-8"))
            if type is not None and data.get("type") != type:
                continue
            tags = data.get("tags") or []
            if isinstance(tags, str):  # frontmatter `tags: backend` (a scalar) -> [backend]
                tags = [tags]
            if tag is not None and tag not in tags:
                continue
            out.append({"path": rel, "type": data.get("type"), "tags": tags})
            if len(out) >= limit:
                break
        return out
