from pathlib import Path
import importlib.util


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
    root = Path(__file__).resolve().parents[1] / ".claude" / "skills"
    names = {p.parent.name for p in root.glob("*/SKILL.md")}
    assert names == {
        "canvas",
        "code-graph",
        "code-impact",
        "obsidian-markdown",
        "wiki-curate",
        "wiki-fold",
        "wiki-ingest",
        "wiki-lint",
        "wiki-query",
    }
