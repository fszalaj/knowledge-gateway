from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

EXCLUDE_DIRS = {
    ".obsidian",
    ".trash",
    ".git",
    ".raw",
    ".smart-connections",
    ".obsidian-git-data",
}

# Attachments: non-note binaries embedded in a vault (![[image.png]]). IMAGE_FORMATS maps an
# extension to the FastMCP Image `format` that yields the correct image/<fmt> mime (note
# jpg -> jpeg); everything else in ATTACHMENT_EXTS is returned as a File.
IMAGE_FORMATS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp"}
ATTACHMENT_EXTS = set(IMAGE_FORMATS) | {
    ".bmp", ".svg", ".pdf",
    ".mp3", ".wav", ".m4a", ".ogg", ".flac",
    ".mp4", ".webm", ".mov", ".mkv",
}
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
CANVAS_EXT = ".canvas"  # Obsidian Canvas (JSON: nodes incl. 'group' type, edges, 'color' fields)


@dataclass(frozen=True)
class Vault:
    """A markdown vault plus the git repo that backs it.

    `path` is where the notes live (the Obsidian vault root). `repo_root` is the
    git repository root — equal to `path` when the vault is its own repo, or a
    parent when the vault is a subdirectory of a larger repo. `subdir` is the
    vault location relative to `repo_root`; git operations are pathspec-scoped to
    it, so when `subdir != "."` a commit touches only the vault and never sibling
    code. (`subdir == "."` is only valid when `repo_root == path` — enforced at
    config load.)
    """

    name: str
    path: Path
    repo_root: Path
    subdir: str
    description: str = ""

    def __post_init__(self):
        # Containment compares safe_join's resolve()'d target against these roots, so the
        # roots must be symlink-resolved too. from_spec already resolves; this also covers
        # direct construction and keeps the guard correct for any caller.
        object.__setattr__(self, "path", self.path.resolve())
        object.__setattr__(self, "repo_root", self.repo_root.resolve())

    @classmethod
    def from_spec(cls, name: str, spec: dict) -> "Vault":
        path = Path(str(spec["path"])).expanduser().resolve()
        repo_root = Path(str(spec.get("repo_root", spec["path"]))).expanduser().resolve()
        subdir = str(spec.get("subdir", ".")) or "."
        return cls(
            name=name,
            path=path,
            repo_root=repo_root,
            subdir=subdir,
            description=str(spec.get("description", "")),
        )

    def safe_join(self, rel: str) -> Path:
        # resolve() collapses ".." and follows symlinks, so a path escaping the
        # vault — or a symlink pointing outside it — fails the containment check.
        target = (self.path / rel).resolve()
        if target == self.path or not target.is_relative_to(self.path):
            raise PermissionError(f"path_escape: {rel}")
        # Config/system dirs are never valid note targets. Enforcing the same
        # exclusion on read/write keeps a granted token from reading .git/config
        # or .env, or writing .git/config (which would be host code execution).
        if any(part in EXCLUDE_DIRS for part in target.relative_to(self.path).parts):
            raise PermissionError(f"path_excluded: {rel}")
        return target

    def safe_note_path(self, rel: str) -> Path:
        # The read/write surface is markdown notes only. Reject non-.md targets
        # and any hidden path component (.env, secrets, .git, .obsidian, ...), so
        # a granted token cannot reach a config/secret file that happens to sit
        # inside a vault root — not just the EXCLUDE_DIRS set.
        target = self.safe_join(rel)
        if any(part.startswith(".") for part in target.relative_to(self.path).parts):
            raise PermissionError(f"path_hidden: {rel}")
        if target.suffix != ".md":
            raise PermissionError(f"not_a_note: {rel}")
        return target

    def list_markdown(self, subdir: str | None = None, limit: int | None = None) -> list[str]:
        root = self.path if subdir in (None, "", ".") else self.safe_join(subdir)
        out: list[str] = []
        for p in sorted(root.rglob("*.md")):
            rel = p.relative_to(self.path)
            if any(part in EXCLUDE_DIRS or part.startswith(".") for part in rel.parts):
                continue
            out.append(rel.as_posix())
            if limit and len(out) >= limit:
                break
        return out

    def safe_attachment_path(self, rel: str) -> Path:
        # An attachment is a non-note binary (image/pdf/audio/video). Same containment +
        # hidden-component guards as a note, but the allowlist is ATTACHMENT_EXTS, not .md.
        target = self.safe_join(rel)
        if any(part.startswith(".") for part in target.relative_to(self.path).parts):
            raise PermissionError(f"path_hidden: {rel}")
        if target.suffix.lower() not in ATTACHMENT_EXTS:
            raise PermissionError(f"not_an_attachment: {rel}")
        return target

    def list_attachments(self, subdir: str | None = None, limit: int | None = None) -> list[str]:
        root = self.path if subdir in (None, "", ".") else self.safe_join(subdir)
        out: list[str] = []
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in ATTACHMENT_EXTS:
                continue
            rel = p.relative_to(self.path)
            if any(part in EXCLUDE_DIRS or part.startswith(".") for part in rel.parts):
                continue
            out.append(rel.as_posix())
            if limit and len(out) >= limit:
                break
        return out

    def safe_canvas_path(self, rel: str) -> Path:
        # A .canvas file (Obsidian Canvas, JSON). Same containment + hidden guards as a note.
        target = self.safe_join(rel)
        if any(part.startswith(".") for part in target.relative_to(self.path).parts):
            raise PermissionError(f"path_hidden: {rel}")
        if target.suffix.lower() != CANVAS_EXT:
            raise PermissionError(f"not_a_canvas: {rel}")
        return target

    def list_canvases(self, subdir: str | None = None, limit: int | None = None) -> list[str]:
        root = self.path if subdir in (None, "", ".") else self.safe_join(subdir)
        out: list[str] = []
        for p in sorted(root.rglob("*.canvas")):
            rel = p.relative_to(self.path)
            if any(part in EXCLUDE_DIRS or part.startswith(".") for part in rel.parts):
                continue
            out.append(rel.as_posix())
            if limit and len(out) >= limit:
                break
        return out
