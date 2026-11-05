#!/usr/bin/env python3
"""Validate the repository's portable agent-skill package.

Dependency-free by design so it can run before the project environment is installed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The canonical pack is a real directory that Codex, Gemini CLI, GitHub Copilot and Cursor
# discover natively; Claude Code reads a skill file on demand, as AGENTS.md instructs.
SKILLS_REL = Path(".agents") / "skills"
SKILLS_ROOT = ROOT / SKILLS_REL
MANIFEST = SKILLS_ROOT / "manifest.json"
# Regular files that import AGENTS.md. A git symlink checks out as a text placeholder on
# Windows without Developer Mode (or any core.symlinks=false checkout), so agents would get
# no instructions; an import line works everywhere.
INSTRUCTION_FILES = ("CLAUDE.md", "GEMINI.md")
# Earlier layouts: the skills/ and .claude/skills/ aliases, and the Claude Code plugin manifests.
RETIRED_PATHS = (Path("skills"), Path(".claude") / "skills", Path(".claude-plugin"))
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
KNOWN_TOOLS = {
    "list_vaults", "list_notes", "read_note", "list_attachments", "read_attachment",
    "list_canvases", "read_canvas", "write_canvas", "search", "backlinks",
    "list_tags", "query_notes", "write_note", "patch_note", "patch_frontmatter",
    "delete_note", "rename_note", "git_status", "git_commit", "list_graphs",
    "graph_query", "graph_neighbors", "god_nodes", "graph_shortest_path",
    "graph_stats", "graph_build", "convert_to_markdown",
}


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing opening YAML frontmatter fence")
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError("missing closing YAML frontmatter fence") from exc
    data: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"unsupported frontmatter line: {line!r}")
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data, "\n".join(lines[end + 1 :]).strip()


def git_index(root: Path) -> tuple[dict[str, bytes] | None, str | None]:
    """Map each tracked path to its git mode, or (None, reason) when the index is unreadable.

    The reason is None only for a plain "not a git repository" result outside any checkout
    (an unpacked source archive or a test fixture), which is the one case with nothing to check.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--stage", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return None, f"git is not runnable: {exc}"
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip() or f"exit {result.returncode}"
        if "not a git repository" in message and not (root / ".git").exists():
            return None, None
        return None, message
    entries: dict[str, bytes] = {}
    for record in filter(None, result.stdout.split(b"\0")):
        meta, _, path = record.partition(b"\t")
        entries[path.decode("utf-8", "surrogateescape")] = meta.split(b" ", 1)[0]
    return entries, None


def tracked_symlinks(root: Path) -> list[str] | None:
    """Paths git tracks as symlinks (mode 120000), or None when no git index is readable."""
    entries, _ = git_index(root)
    if entries is None:
        return None
    return [path for path, mode in entries.items() if mode == b"120000"]


def validate_tracked_symlinks(root: Path, entries: dict[str, bytes] | None, reason: str | None) -> list[str]:
    if entries is None:
        if reason is None:  # an unpacked archive or a fixture outside any checkout
            return []
        if os.environ.get("CI"):
            return [f"cannot read the git index to enforce the no-symlink rule: {reason}"]
        print(f"warning: no-symlink check skipped, git index unreadable: {reason}", file=sys.stderr)
        return []
    return [f"{path}: git tracks a symlink; use a regular file so every OS checks it out"
            for path, mode in entries.items() if mode == b"120000"]


def validate_retired_paths(root: Path, entries: dict[str, bytes] | None) -> list[str]:
    """Reject the pre-portable layout without flagging a developer's own untracked skills.

    `.claude/skills/` is Claude Code's standard place for personal project skills, and branch
    switches can leave untracked files under `skills/`; only tracked entries, symlinks and
    checked-out placeholder files mean the old layout came back.
    """
    errors: list[str] = []
    for retired in RETIRED_PATHS:
        rel = retired.as_posix()
        path = root / retired
        tracked = entries is not None and any(p == rel or p.startswith(rel + "/") for p in entries)
        if tracked or path.is_symlink() or path.is_file():
            errors.append(f"{rel} is retired; the canonical pack is {SKILLS_REL.as_posix()}")
    return errors


def _inside(root: Path, rel: str) -> Path | None:
    """Resolve a repository-relative reference, or None when it escapes the repository."""
    if Path(rel).is_absolute() or "\\" in rel:
        return None
    target = (root / rel).resolve()
    return target if target.is_relative_to(root.resolve()) else None


def import_lines(text: str) -> list[str]:
    """Targets of `@path` import lines outside fenced code blocks, which clients do not expand."""
    targets: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
            continue
        if fence is None and stripped.startswith("@"):
            targets.append(stripped[1:].strip())
    return targets


def validate_instruction_files(root: Path) -> list[str]:
    errors: list[str] = []
    agents = root / "AGENTS.md"
    if agents.is_symlink() or not agents.is_file():
        return ["AGENTS.md must be a regular file"]
    for name in INSTRUCTION_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            errors.append(f"{name} must be a regular file that imports AGENTS.md")
            continue
        imports = import_lines(path.read_text(encoding="utf-8"))
        if not any(_inside(root, ref) == agents.resolve() for ref in imports):
            errors.append(f"{name} must contain an import line for AGENTS.md (e.g. @AGENTS.md)")
    return errors


def validate(root: Path = ROOT) -> list[str]:
    entries, reason = git_index(root)
    return (validate_tracked_symlinks(root, entries, reason) + validate_instruction_files(root)
            + validate_retired_paths(root, entries) + validate_pack(root))


def validate_pack(root: Path) -> list[str]:
    errors: list[str] = []
    skills_root = root / SKILLS_REL
    if skills_root.is_symlink() or not skills_root.is_dir():
        return [f"{SKILLS_REL.as_posix()} must be a real directory holding the canonical pack"]
    manifest_path = skills_root / "manifest.json"
    if not manifest_path.is_file():
        return [f"missing manifest: {manifest_path.relative_to(root)}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return [f"invalid manifest JSON: {exc}"]
    if manifest.get("schema_version") != 1:
        errors.append("manifest schema_version must be 1")
    entries = manifest.get("skills")
    if not isinstance(entries, list) or not entries:
        return errors + ["manifest skills must be a non-empty list"]

    seen: set[str] = set()
    manifest_paths: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("manifest skill entry must be an object")
            continue
        name = entry.get("name")
        rel = entry.get("path")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            errors.append(f"invalid skill name: {name!r}")
            continue
        if name in seen:
            errors.append(f"duplicate skill name: {name}")
        seen.add(name)
        # Relative to the pack, so the manifest holds in the repository (.agents/skills), in the
        # sdist (skills/) and in consumer copies alike.
        expected_rel = f"{name}/SKILL.md"
        if rel != expected_rel:
            errors.append(f"{name}: path must be {expected_rel} (relative to the pack)")
        path = skills_root / expected_rel
        manifest_paths.add(path)
        if not path.is_file():
            errors.append(f"{name}: missing {(SKILLS_REL / expected_rel).as_posix()}")
            continue
        try:
            fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        if fm.get("name") != name:
            errors.append(f"{name}: frontmatter name must match directory")
        extra_keys = sorted(set(fm) - {"name", "description"})
        if extra_keys:
            errors.append(f"{name}: unsupported frontmatter keys: {', '.join(extra_keys)}")
        if len(fm.get("description", "")) < 40:
            errors.append(f"{name}: description must explain when the skill is used")
        if not body.startswith("# "):
            errors.append(f"{name}: body must start with one H1 heading")
        if re.search(r"(?<![A-Za-z0-9])/(?:Users|home|tmp)/", body):
            errors.append(f"{name}: contains a machine-specific absolute path")
        required = entry.get("requires", [])
        optional = entry.get("optional_requires", [])
        if any(not isinstance(items, list) or any(not isinstance(x, str) for x in items)
               for items in (required, optional)):
            errors.append(f"{name}: requires and optional_requires must be string lists")
        else:
            declared = set(required) | set(optional)
            unknown = sorted(declared - KNOWN_TOOLS)
            if unknown:
                errors.append(f"{name}: unknown tools in manifest: {', '.join(unknown)}")
            for tool in declared:
                if f"`{tool}`" not in body:
                    errors.append(f"{name}: declared tool `{tool}` is not documented in SKILL.md")
            undeclared = sorted(tool for tool in KNOWN_TOOLS if f"`{tool}`" in body and tool not in declared)
            if undeclared:
                errors.append(f"{name}: documented tools missing from manifest: {', '.join(undeclared)}")

    discovered = set(skills_root.glob("*/SKILL.md"))
    for path in sorted(discovered - manifest_paths):
        errors.append(f"unregistered skill: {path.relative_to(root).as_posix()}")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        print("Agent skill validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    count = len(json.loads(MANIFEST.read_text(encoding="utf-8"))["skills"])
    print(f"Agent skill validation OK: {count} skills")
    return 0


if __name__ == "__main__":
    sys.exit(main())
