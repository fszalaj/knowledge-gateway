import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import tarfile
import tomllib


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


def test_expected_agent_skills_are_present():
    root = Path(__file__).resolve().parents[1] / "skills"
    names = {p.parent.name for p in root.glob("*/SKILL.md")}
    assert names == {
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
        "wiki-ingest",
        "wiki-lint",
        "wiki-query",
    }


def test_provider_aliases_point_to_canonical_pack():
    root = Path(__file__).resolve().parents[1]
    for relative in (Path(".agents/skills"), Path(".claude/skills")):
        alias = root / relative
        assert alias.is_symlink()
        assert os.readlink(alias) == "../skills"


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
    assert any(name.endswith("/skills/manifest.json") for name in names)
    assert not any("/.agents/" in name or "/.claude/" in name for name in names)


def test_convert_extra_installs_pdf_and_office_converters():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    expected = "markitdown[pdf,docx,pptx,xlsx,xls,outlook]>=0.1"
    assert project["optional-dependencies"]["convert"] == [expected]
    assert expected in project["optional-dependencies"]["all"]
