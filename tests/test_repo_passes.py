"""The dedicated repo-level passes wired together through build_graph.

Each pass has its own tests; these check what only the integration can get wrong: every pass
runs and is reported in `graph.passes`, ids written by one pass land on the node another pass
owns (so the graphs join instead of growing duplicates), no value reaches the merged graph, and
the merged output is deterministic.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("networkx")

from gateway.codegraph import build_graph
from gateway.codegraph.build import REPO_PASSES

CANARY = "zq-canary-4f7e91c2-not-a-real-value"


def _mk(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _repo(root):
    # Python app + manifest
    _mk(root, "pyproject.toml",
        f"[project]\nname = 'app'\ndependencies = ['requests>=2']\n\n[tool.app]\ntoken = '{CANARY}'\n")
    _mk(root, "app/main.py", "def main():\n    return 1\n")
    # Docker + compose
    _mk(root, "Dockerfile",
        f"FROM python:3.12-slim AS app\nARG TOKEN={CANARY}\nENV TOKEN={CANARY}\n"
        "WORKDIR /srv\nCOPY pyproject.toml ./\nCOPY app/main.py /srv/main.py\n"
        "CMD [\"python\", \"/srv/main.py\"]\n")
    _mk(root, "compose.yaml",
        f"services:\n  web:\n    build: .\n    environment:\n      TOKEN: {CANARY}\n")
    # GitHub Actions
    _mk(root, ".github/workflows/ci.yml",
        "name: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"      - run: docker build -t app .\n        env:\n          TOKEN: ${{{{ secrets.APP_TOKEN }}}}\n"
        f"          PLAIN: {CANARY}\n")
    # Bicep + its deploy script
    _mk(root, "infra/main.bicep",
        f"@secure()\nparam adminPassword string = '{CANARY}'\nparam location string\n"
        "resource store 'Microsoft.Storage/storageAccounts@2023-01-01' = {\n"
        "  name: 'store'\n  location: location\n}\noutput storeId string = store.id\n")
    _mk(root, "infra/dev.bicepparam",
        f"using 'main.bicep'\nparam location = '{CANARY}'\nparam adminPassword = '{CANARY}'\n")
    _mk(root, "infra/deploy.sh",
        "#!/usr/bin/env bash\naz deployment group create -g rg "
        "--parameters \"$(dirname \"$0\")/dev.bicepparam\"\n")
    # Helm chart + a plain manifest Secret
    _mk(root, "chart/web/Chart.yaml", "apiVersion: v2\nname: web\nversion: 0.1.0\n")
    _mk(root, "chart/web/values.yaml", f"replicas: 1\npassword: {CANARY}\n")
    _mk(root, "chart/web/templates/svc.yaml",
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: web\nspec:\n"
        "  replicas: {{ .Values.replicas }}\n  selector:\n    app: web\n")
    _mk(root, "manifests/secret.yaml",
        f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: app-secret\nstringData:\n  token: {CANARY}\n")
    # Ansible with an inventory and group vars
    _mk(root, "site.yml", "- hosts: web\n  tasks:\n    - name: ping\n      ping:\n")
    _mk(root, "inventory/hosts", "[web]\nweb1.example.com\n")
    _mk(root, "inventory/group_vars/web.yml", f"token: {CANARY}\n")


def _index(data):
    nodes = {n["id"]: n for n in data["nodes"]}
    edges = {(e["source"], r, e["target"]) for e in data["links"]
             for r in (e.get("relations") or [e["relation"]])}
    return nodes, edges


def test_every_repo_pass_runs_and_is_reported(tmp_path):
    _repo(tmp_path)
    data = build_graph(tmp_path)
    passes = data["graph"]["passes"]
    assert list(passes) == [name for name, _ in REPO_PASSES]
    for name in ("ansible", "bicep", "docker", "ghactions", "k8s", "manifests"):
        assert passes[name] > 0, name
    assert passes["fabric"] == 0 and passes["appsmith"] == 0   # ran, found nothing


def test_passes_join_on_each_others_ids(tmp_path):
    _repo(tmp_path)
    nodes, edges = _index(build_graph(tmp_path))
    # CI builds the image the docker pass graphed: one dockerfile node, with its stages
    assert any(r == "runs_script" and t == "dockerfile:Dockerfile" for _, r, t in edges)
    assert nodes["dockerfile:Dockerfile"]["type"] == "dockerfile"
    assert any(s == "dockerfile:Dockerfile" and r == "has_stage" for s, r, _ in edges)
    # the image copies the manifest the manifests pass parsed
    assert any(r == "copies" and t == "manifest:pyproject.toml" for _, r, t in edges)
    assert ("manifest:pyproject.toml", "depends_on", "extpackage:pypi:requests") in edges
    # and runs the module the python pass parsed
    assert any(r == "runs" and t == "module:app/main.py" for _, r, t in edges)
    assert any(s == "module:app/main.py" and r == "defines" for s, r, _ in edges) or \
        "pyfunc:app/main.py:main" in nodes
    # the deploy script is the same module:<rel> node the tree-sitter pass would write
    script = nodes["module:infra/deploy.sh"]
    assert (script["type"], script["file_type"]) == ("module", "bash")
    assert ("module:infra/deploy.sh", "deploys", "bicep:infra/dev.bicepparam") in edges
    assert ("module:infra/deploy.sh", "deploys", "bicep:infra/main.bicep") in edges
    # no edge points at a node nobody typed (a dangling join)
    untyped = [i for i, n in nodes.items() if "type" not in n]
    assert not [i for i in untyped if i.split(":", 1)[0] in
                {"module", "dockerfile", "manifest", "playbook", "bicep", "bicepparam", "helmchart"}]


def test_two_relations_on_one_pair_are_both_kept(tmp_path):
    # the stage copies main.py and then runs it: one DiGraph edge, both relation names
    _repo(tmp_path)
    data = build_graph(tmp_path)
    links = [e for e in data["links"] if e["target"] == "module:app/main.py"
             and e["source"].startswith("dockerstage:")]
    assert len(links) == 1
    assert links[0]["relations"] == ["copies", "runs"]
    assert links[0]["relation"] in ("copies", "runs")
    # a pair that carries one relation gets no `relations` list
    assert not any("relations" in e for e in data["links"] if e["relation"] == "has_stage")


def test_repo_passes_need_no_tree_sitter(tmp_path, monkeypatch):
    # [graph-slim]: the parser pack is absent, every dedicated pass still runs and the script
    # and Dockerfile nodes they reference still carry a type
    from gateway.codegraph import treesitter
    monkeypatch.setattr(treesitter, "AVAILABLE", False)
    _repo(tmp_path)
    data = build_graph(tmp_path)
    assert data["graph"]["treesitter_available"] is False
    assert data["graph"]["passes"]["bicep"] > 0 and data["graph"]["passes"]["docker"] > 0
    nodes, _ = _index(data)
    assert nodes["module:infra/deploy.sh"]["type"] == "module"
    assert nodes["dockerfile:Dockerfile"]["type"] == "dockerfile"


@pytest.mark.parametrize("slim", ["no_tree_sitter", "python_only"])
def test_appsmith_jsobject_target_is_typed_without_tree_sitter(tmp_path, monkeypatch, slim):
    # implemented_by points at the .js file's module:<rel>; the pass must write that node
    # itself, or under [graph-slim] / --languages python it is an untyped placeholder
    from gateway.codegraph import treesitter
    if slim == "no_tree_sitter":
        monkeypatch.setattr(treesitter, "AVAILABLE", False)
    _mk(tmp_path, "ui/metadata.json", '{"artifactJsonType": "APPLICATION"}')
    _mk(tmp_path, "ui/application.json", '{"name": "ui"}')
    _mk(tmp_path, "ui/pages/P/P.json", '{"unpublishedPage": {"name": "P"}}')
    _mk(tmp_path, "ui/pages/P/jsobjects/J/J.js", "export default { f() { return 1 } }\n")
    data = build_graph(tmp_path, languages=["python"] if slim == "python_only" else None)
    nodes, edges = _index(data)
    mod = "module:ui/pages/P/jsobjects/J/J.js"
    assert ("appsmithjs:ui/P/J", "implemented_by", mod) in edges
    assert (nodes[mod]["type"], nodes[mod]["file_type"]) == ("module", "javascript")
    assert not [i for i, n in nodes.items() if "type" not in n]


def test_a_failing_pass_costs_its_fragment_not_the_build(tmp_path, monkeypatch):
    from gateway.codegraph import build

    def boom(root, exclude=frozenset(), include=frozenset()):
        raise RuntimeError("unforeseen input")
    passes = tuple((n, boom if n == "docker" else f) for n, f in build.REPO_PASSES)
    monkeypatch.setattr(build, "REPO_PASSES", passes)
    _repo(tmp_path)
    data = build_graph(tmp_path)
    assert data["graph"]["passes"]["docker"] == -1
    assert data["graph"]["passes"]["bicep"] > 0
    assert not any(n["id"].startswith("dockerstage:") for n in data["nodes"])


def test_no_value_reaches_the_merged_graph(tmp_path):
    _repo(tmp_path)
    assert CANARY not in json.dumps(build_graph(tmp_path))


def test_merged_output_is_deterministic(tmp_path):
    _repo(tmp_path)
    first = json.dumps(build_graph(tmp_path), sort_keys=True)
    assert json.dumps(build_graph(tmp_path), sort_keys=True) == first
