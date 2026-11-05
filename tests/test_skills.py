import ast
import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _validator_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "validate-skills.py"
    spec = importlib.util.spec_from_file_location("validate_skills", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_skill_package_is_valid():
    validator = _validator_module()
    assert validator.validate() == []


def test_bundled_agent_skills_are_present():
    # A superset is allowed: a downstream repository that keeps this tree (a team vault, a fork) adds its own
    # skills to the same pack; the validator holds every registered skill to the same rules.
    root = ROOT / ".agents" / "skills"
    names = {p.parent.name for p in root.glob("*/SKILL.md")}
    assert names >= {
        "canvas",
        "code-graph-build",
        "code-graph-explore",
        "code-impact",
        "cordis-composability",
        "document-convert",
        "gateway-operations",
        "gateway-setup",
        "obsidian-markdown",
        "wiki-curate",
        "wiki-fold",
        "vault-roster",
        "wiki-ingest",
        "wiki-lint",
        "wiki-query",
    }


def test_canonical_pack_is_a_real_directory_registered_in_its_manifest():
    pack = ROOT / ".agents" / "skills"
    assert pack.is_dir() and not pack.is_symlink()
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    registered = {entry["path"] for entry in manifest["skills"]}
    # Pack-relative, so the same manifest is correct in the sdist's skills/ directory.
    assert registered == {f"{p.parent.name}/SKILL.md" for p in pack.glob("*/SKILL.md")}
    # The pre-portable locations must not come back as placeholders or aliases.
    for retired in (ROOT / "skills", ROOT / ".claude" / "skills"):
        assert not retired.is_symlink() and not retired.is_file()
    # Claude Code reads the pack as AGENTS.md instructs; there is no plugin manifest to keep in step.
    assert not (ROOT / ".claude-plugin").exists()


def test_instruction_files_are_regular_imports_of_agents_md():
    # A git symlink checks out as a 9-byte text placeholder without Developer Mode on Windows
    # or under core.symlinks=false; an import line loads the instructions everywhere.
    for name, line in (("CLAUDE.md", "@AGENTS.md"), ("GEMINI.md", "@./AGENTS.md")):
        path = ROOT / name
        assert path.is_file() and not path.is_symlink()
        assert line in path.read_text(encoding="utf-8").splitlines()


def test_repository_tracks_no_symlinks():
    links = _validator_module().tracked_symlinks(ROOT)
    if links is None:
        # CI must enforce the rule; a local worktree whose index this platform cannot read skips.
        assert not os.environ.get("CI"), "git index unreadable in CI: the no-symlink rule went unchecked"
        pytest.skip("git index of this checkout is not readable here")
    assert links == []


def test_unreadable_git_index_fails_in_ci_and_warns_locally(tmp_path, monkeypatch, capsys):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = _portable_fixture(tmp_path)
    (root / ".git").write_text("gitdir: /nonexistent/worktree\n", encoding="utf-8")  # a broken worktree link
    validator = _validator_module()
    monkeypatch.delenv("CI", raising=False)
    assert validator.validate(root) == []
    assert "no-symlink check skipped" in capsys.readouterr().err
    monkeypatch.setenv("CI", "true")
    assert any("cannot read the git index" in error for error in validator.validate(root))


def test_fixture_outside_any_checkout_is_not_a_git_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert _validator_module().validate(_portable_fixture(tmp_path)) == []


def _portable_fixture(tmp_path):
    for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md", "pyproject.toml"):
        shutil.copy(ROOT / name, tmp_path / name)
    shutil.copytree(ROOT / ".agents" / "skills", tmp_path / ".agents" / "skills")
    return tmp_path


def test_validator_accepts_the_portable_layout(tmp_path):
    assert _validator_module().validate(_portable_fixture(tmp_path)) == []


def _add_downstream_skill(root, register=True, body="# Team runbook\n\nUse `read_note` to load the runbook page.\n"):
    skill = root / ".agents" / "skills" / "team-runbook"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: team-runbook\ndescription: Follow a team-specific runbook kept in the vault when operating a service.\n"
        "---\n\n" + body, encoding="utf-8")
    if register:
        manifest_path = root / ".agents" / "skills" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["skills"].append({"name": "team-runbook", "path": "team-runbook/SKILL.md", "mode": "read-only",
                                   "requires": ["read_note"], "optional_requires": []})
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def test_validator_accepts_a_registered_downstream_skill(tmp_path):
    root = _portable_fixture(tmp_path)
    _add_downstream_skill(root)
    assert _validator_module().validate(root) == []


def test_validator_holds_a_downstream_skill_to_the_same_rules(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    unregistered = _portable_fixture(tmp_path / "a")
    _add_downstream_skill(unregistered, register=False)
    assert "unregistered skill: .agents/skills/team-runbook/SKILL.md" in _validator_module().validate(unregistered)
    undocumented = _portable_fixture(tmp_path / "b")
    _add_downstream_skill(undocumented, body="# Team runbook\n\nNo tool is named here.\n")
    errors = _validator_module().validate(undocumented)
    assert any("declared tool `read_note` is not documented" in e for e in errors)


@pytest.mark.parametrize("content", [
    "AGENTS.md\n", "@README.md\n", "@../AGENTS.md\n", "",
    "```markdown\n@AGENTS.md\n```\n",  # imports inside a code fence are not expanded
    "~~~\n@AGENTS.md\n~~~\n",
])
def test_validator_rejects_an_instruction_file_without_an_agents_import(tmp_path, content):
    root = _portable_fixture(tmp_path)
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text(content, encoding="utf-8")
    assert any(error.startswith("CLAUDE.md") for error in _validator_module().validate(root))


@pytest.mark.parametrize("retired", ["skills", ".claude/skills"])
def test_validator_rejects_retired_skill_locations(tmp_path, retired):
    root = _portable_fixture(tmp_path)
    (root / retired).parent.mkdir(parents=True, exist_ok=True)
    (root / retired).write_text("../skills", encoding="utf-8")  # a checked-out placeholder
    assert any("is retired" in error for error in _validator_module().validate(root))


@pytest.mark.parametrize("retired", ["skills", ".claude/skills"])
def test_validator_allows_untracked_local_skills_in_retired_locations(tmp_path, retired):
    # .claude/skills is Claude Code's own place for a developer's personal project skills, and
    # a branch switch can leave untracked files under skills/.
    root = _portable_fixture(tmp_path)
    local = root / retired / "my-local-skill"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("---\nname: my-local-skill\n---\n# Local\n", encoding="utf-8")
    assert _validator_module().validate(root) == []


def _git_fixture(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = _portable_fixture(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _track(root, path, content=b"x", mode="100644"):
    blob = subprocess.run(["git", "-C", str(root), "hash-object", "-w", "--stdin"], input=content,
                          capture_output=True, check=True).stdout.decode().strip()
    # Record the entry in the index directly, so the test needs neither the file nor symlink support.
    subprocess.run(["git", "-C", str(root), "update-index", "--add", "--cacheinfo",
                    f"{mode},{blob},{path}"], check=True)


@pytest.mark.parametrize("path", ["skills/wiki-query/SKILL.md", ".claude/skills/wiki-query/SKILL.md",
                                  ".claude-plugin/plugin.json"])
def test_validator_rejects_tracked_files_in_retired_locations(tmp_path, path):
    root = _git_fixture(tmp_path)
    _track(root, path)
    assert any("is retired" in error for error in _validator_module().validate(root))


@pytest.mark.parametrize("path", [".mcp.json", ".claude/settings.json"])
def test_validator_allows_project_client_configuration(tmp_path, path):
    # Without a plugin rooted here, a project .mcp.json or Claude Code settings file is ordinary config.
    root = _git_fixture(tmp_path)
    _track(root, path, b"{}")
    assert _validator_module().validate(root) == []


def test_validator_rejects_a_tracked_symlink(tmp_path):
    root = _git_fixture(tmp_path)
    _track(root, "LINK.md", b"AGENTS.md", mode="120000")
    validator = _validator_module()
    assert validator.tracked_symlinks(root) == ["LINK.md"]
    assert any(error.startswith("LINK.md: git tracks a symlink") for error in validator.validate(root))


def test_skill_tool_allowlist_matches_registered_surface():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "gateway" / "tools.py").read_text(encoding="utf-8"))
    registered = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(decorator, ast.Name) and decorator.id in {"tool", "wtool"}
                for decorator in node.decorator_list)
    }
    assert _validator_module().KNOWN_TOOLS == registered


def test_sdist_contains_canonical_skills_not_provider_aliases(tmp_path):
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["uv", "build", "--sdist", "--out-dir", str(tmp_path)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    archive = next(tmp_path.glob("knowledge_gateway-*.tar.gz"))
    with tarfile.open(archive) as package:
        names = package.getnames()
        prefix = archive.name.removesuffix(".tar.gz")
        manifest = json.loads(package.extractfile(f"{prefix}/skills/manifest.json").read())
    # The pack moved to .agents/skills in the repository; the sdist keeps shipping it at skills/,
    # and the pack-relative manifest paths resolve there.
    assert any(name.endswith("/skills/wiki-query/SKILL.md") for name in names)
    assert all(f"{prefix}/skills/{entry['path']}" in names for entry in manifest["skills"])
    assert not any("/.agents/" in name or "/.claude/" in name for name in names)


def test_convert_extra_installs_pdf_and_office_converters():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    expected = "markitdown[pdf,docx,pptx,xlsx,xls,outlook]>=0.1"
    assert project["optional-dependencies"]["convert"] == [expected]
    assert expected in project["optional-dependencies"]["all"]


def test_server_manifest_version_matches_the_package():
    # The release procedure bumps server.json by hand, so nothing caught it drifting two
    # releases behind the package it points registry clients at.
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    manifest = json.loads((root / "server.json").read_text(encoding="utf-8"))
    assert manifest["version"] == version
    assert [p["version"] for p in manifest["packages"]] == [version]
