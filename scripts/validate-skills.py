#!/usr/bin/env python3
"""Validate the repository's portable agent-skill package.

Dependency-free by design so it can run before the project environment is installed.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / ".claude" / "skills"
MANIFEST = SKILLS_ROOT / "manifest.json"
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


def validate(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    skills_root = root / ".claude" / "skills"
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
        expected_rel = Path(".claude") / "skills" / name / "SKILL.md"
        if rel != expected_rel.as_posix():
            errors.append(f"{name}: path must be {expected_rel.as_posix()}")
        path = root / expected_rel
        manifest_paths.add(path)
        if not path.is_file():
            errors.append(f"{name}: missing {expected_rel.as_posix()}")
            continue
        try:
            fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        if fm.get("name") != name:
            errors.append(f"{name}: frontmatter name must match directory")
        if len(fm.get("description", "")) < 40:
            errors.append(f"{name}: description must explain when the skill is used")
        if not body.startswith("# "):
            errors.append(f"{name}: body must start with one H1 heading")
        if re.search(r"(?:^|\s)/(?:Users|home|tmp)/", body):
            errors.append(f"{name}: contains a machine-specific absolute path")
        required = entry.get("requires", [])
        if not isinstance(required, list) or any(not isinstance(x, str) for x in required):
            errors.append(f"{name}: requires must be a string list")
        else:
            unknown = sorted(set(required) - KNOWN_TOOLS)
            if unknown:
                errors.append(f"{name}: unknown tools in manifest: {', '.join(unknown)}")
            for tool in required:
                if f"`{tool}`" not in body:
                    errors.append(f"{name}: required tool `{tool}` is not documented in SKILL.md")

    discovered = set(skills_root.glob("*/SKILL.md"))
    for path in sorted(discovered - manifest_paths):
        errors.append(f"unregistered skill: {path.relative_to(root)}")
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
