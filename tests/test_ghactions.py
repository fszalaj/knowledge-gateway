"""GitHub Actions extractor: workflows, jobs, actions and the local files CI runs."""
from __future__ import annotations

import json
import os

import pytest

from gateway.codegraph import extract_ansible, extract_ghactions

PLANTED = "plantedValue-7f3c9e1a-do-not-leak"


def _mk(root, rel, text=""):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _triples(frag):
    return {(e["source"], e["relation"], e["target"]) for e in frag["edges"]}


def _nodes(frag):
    return {n["id"]: n for n in frag["nodes"]}


CI = f"""\
name: CI
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
    inputs:
      level:
        default: {PLANTED}
env:
  API_TOKEN: {PLANTED}
  DEPLOY_KEY: ${{{{ secrets.DEPLOY_KEY }}}}
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: example-org/setup-tool/sub/dir@v1.2.0
      - uses: ./.github/actions/setup
        with:
          password: {PLANTED}
      - run: bin/lint.sh
      - run: |
          set -eu
          bash scripts/check.sh --strict
          python3 -u tools/gen.py --out build/x
          uv run --with requests tools/report.py
          echo "done" > /dev/null 2>&1
  build:
    name: Build image
    needs: lint
    runs-on: [self-hosted, linux]
    environment:
      name: production
      url: https://{PLANTED}.example.com
    defaults:
      run:
        working-directory: deploy
    steps:
      - run: |
          docker build \\
            ${{{{ github.ref == 'refs/heads/main' && format('-t {{0}}', 'x') || '' }}}} \\
            -f docker/web.Dockerfile .
          docker build ..
      - run: ./run.sh
        env:
          PASSWORD: ${{{{ secrets.DB_PASSWORD }}}}
          LEVEL: ${{{{ vars.LOG_LEVEL }}}}
      - if: vars.ENABLE_PUSH == 'true'
        run: echo push
  infra:
    needs: [lint, build, ghost]
    runs-on: ubuntu-latest
    steps:
      - run: terraform -chdir=infra plan -var "pw=${{{{ secrets.TF_PW }}}}"
      - run: cd packer && packer build -var-file=vars.pkrvars.hcl .
      - run: ansible-playbook -i inventory/hosts -e "x={PLANTED}" site.yml
      - run: pwsh -NoProfile -File scripts/win.ps1
      - run: missing/nothing.sh
  call-local:
    uses: ./.github/workflows/reusable.yml
    secrets: inherit
  call-remote:
    uses: example-org/shared/.github/workflows/release.yml@v2
    with:
      token: {PLANTED}
"""

REUSABLE = """\
on:
  workflow_call:
    secrets:
      token:
        required: true
jobs:
  go:
    runs-on: ubuntu-latest
    steps:
      - uses: docker://alpine:3.20
      - run: "${{ github.workspace }}/bin/lint.sh"
"""

SETUP_ACTION = """\
name: Setup
inputs:
  version:
    default: "1.0"
runs:
  using: composite
  steps:
    - uses: actions/setup-python@v5
    - uses: actions/checkout@v3
    - uses: actions/checkout@v4
    - run: ${{ github.action_path }}/install.sh
      shell: bash
    - uses: ./tools/inner
"""

INNER_ACTION = """\
name: Inner
runs:
  using: node20
  main: dist/index.js
"""


def _repo(root):
    _mk(root, ".github/workflows/ci.yml", CI)
    _mk(root, ".github/workflows/reusable.yml", REUSABLE)
    _mk(root, ".github/workflows/README.md", "not a workflow\n")
    _mk(root, ".github/actions/setup/action.yml", SETUP_ACTION)
    _mk(root, ".github/actions/setup/install.sh", "#!/bin/sh\n")
    _mk(root, "tools/inner/action.yml", INNER_ACTION)
    _mk(root, "tools/inner/dist/index.js", "module.exports = 1\n")
    _mk(root, "bin/lint.sh", "#!/bin/sh\n")
    _mk(root, "scripts/check.sh", "#!/bin/sh\n")
    _mk(root, "scripts/win.ps1", "Write-Output hi\n")
    _mk(root, "tools/gen.py", "print(1)\n")
    _mk(root, "tools/report.py", "print(2)\n")
    _mk(root, "deploy/docker/web.Dockerfile", "FROM scratch\n")
    _mk(root, "deploy/run.sh", "#!/bin/sh\n")
    _mk(root, "Dockerfile", "FROM scratch\n")
    _mk(root, "infra/main.tf", 'resource "null_resource" "x" {}\n')
    _mk(root, "infra/vars.tf", 'variable "y" {}\n')
    _mk(root, "infra/notes.txt", "x\n")
    _mk(root, "packer/image.pkr.hcl", "source \"null\" \"x\" {}\n")
    _mk(root, "packer/vars.pkrvars.hcl", "x = 1\n")
    _mk(root, "site.yml", "- hosts: all\n  roles:\n    - web\n")


def test_workflow_jobs_and_triggers(tmp_path):
    _repo(tmp_path)
    frag = extract_ghactions.extract(tmp_path)
    n = _nodes(frag)
    wf = n["ghworkflow:.github/workflows/ci.yml"]
    assert wf["label"] == "CI" and wf["source_file"] == ".github/workflows/ci.yml"
    assert wf["triggers"] == ["push", "pull_request", "workflow_dispatch"]  # bare `on` is True
    assert n["ghworkflow:.github/workflows/reusable.yml"]["triggers"] == ["workflow_call"]
    assert n["ghworkflow:.github/workflows/reusable.yml"]["label"] == "reusable.yml"
    assert not any("README" in i for i in n)

    lint = n["ghjob:.github/workflows/ci.yml#lint"]
    assert lint["runs_on"] == "ubuntu-latest" and lint["source_location"].startswith("L")
    build = n["ghjob:.github/workflows/ci.yml#build"]
    assert build["label"] == "Build image"
    assert build["runs_on"] == ["self-hosted", "linux"]
    assert build["environment"] == "production"

    t = _triples(frag)
    wf_id = "ghworkflow:.github/workflows/ci.yml"
    for job in ("lint", "build", "infra", "call-local", "call-remote"):
        assert (wf_id, "contains", f"ghjob:.github/workflows/ci.yml#{job}") in t
    j = "ghjob:.github/workflows/ci.yml#"
    assert (j + "build", "depends_on", j + "lint") in t
    assert (j + "infra", "depends_on", j + "lint") in t and (j + "infra", "depends_on", j + "build") in t
    assert not any(e[2].endswith("#ghost") for e in t)  # unknown need: no phantom job


def test_actions_and_reusable_workflows(tmp_path):
    _repo(tmp_path)
    frag = extract_ghactions.extract(tmp_path)
    n = _nodes(frag)
    t = _triples(frag)
    j = "ghjob:.github/workflows/ci.yml#"
    assert (j + "lint", "uses_action", "extaction:actions/checkout") in t
    assert (j + "lint", "uses_action", "extaction:example-org/setup-tool/sub/dir") in t
    assert (j + "lint", "uses_action", "ghaction:.github/actions/setup") in t
    refs = {(e["source"], e["target"]): e.get("ref") for e in frag["edges"]}
    assert refs[(j + "lint", "extaction:actions/checkout")] == "v4"  # ref on the edge, not the id
    assert refs[(j + "lint", "extaction:example-org/setup-tool/sub/dir")] == "v1.2.0"

    setup = n["ghaction:.github/actions/setup"]
    assert setup["label"] == "Setup" and setup["using"] == "composite"
    assert setup["source_file"] == ".github/actions/setup/action.yml"
    assert ("ghaction:.github/actions/setup", "uses_action", "extaction:actions/setup-python") in t
    checkout = [e for e in frag["edges"] if e["source"] == "ghaction:.github/actions/setup"
                and e["target"] == "extaction:actions/checkout"]
    assert len(checkout) == 1 and checkout[0]["refs"] == ["v3", "v4"]
    # github.action_path resolves against the action's own directory
    assert ("ghaction:.github/actions/setup", "runs_script",
            "file:.github/actions/setup/install.sh") not in t
    assert ("ghaction:.github/actions/setup", "runs_script",
            "module:.github/actions/setup/install.sh") in t
    # a local action reached only through another action is still read
    assert ("ghaction:.github/actions/setup", "uses_action", "ghaction:tools/inner") in t
    assert n["ghaction:tools/inner"]["using"] == "node20"
    assert ("ghaction:tools/inner", "runs_script", "module:tools/inner/dist/index.js") in t

    assert (j + "call-local", "calls_workflow", "ghworkflow:.github/workflows/reusable.yml") in t
    assert (j + "call-remote", "calls_workflow",
            "extworkflow:example-org/shared/.github/workflows/release.yml") in t
    calls = {e["target"]: e for e in frag["edges"] if e["relation"] == "calls_workflow"}
    assert calls["ghworkflow:.github/workflows/reusable.yml"]["inherit_secrets"] is True
    assert calls["extworkflow:example-org/shared/.github/workflows/release.yml"]["ref"] == "v2"
    go = "ghjob:.github/workflows/reusable.yml#go"
    assert (go, "uses_action", "extaction:docker://alpine") in t
    assert n["extworkflow:example-org/shared/.github/workflows/release.yml"]["type"] == "external_workflow"


def test_secrets_and_vars_are_names(tmp_path):
    _repo(tmp_path)
    frag = extract_ghactions.extract(tmp_path)
    n = _nodes(frag)
    t = _triples(frag)
    j = "ghjob:.github/workflows/ci.yml#"
    assert ("ghworkflow:.github/workflows/ci.yml", "uses_secret", "ghsecret:DEPLOY_KEY") in t
    assert (j + "build", "uses_secret", "ghsecret:DB_PASSWORD") in t
    assert (j + "build", "uses_var", "ghvar:LOG_LEVEL") in t
    assert (j + "build", "uses_var", "ghvar:ENABLE_PUSH") in t  # bare `if:` expression
    assert (j + "infra", "uses_secret", "ghsecret:TF_PW") in t
    assert n["ghsecret:DB_PASSWORD"] == {"id": "ghsecret:DB_PASSWORD", "label": "DB_PASSWORD",
                                        "type": "secret", "file_type": "github_actions",
                                        "source_file": ".github/workflows/ci.yml"}
    # env names that hold literal values are not secrets and are not graphed
    assert "ghsecret:API_TOKEN" not in n and "ghvar:API_TOKEN" not in n


def test_no_values_leak(tmp_path):
    _repo(tmp_path)
    dumped = json.dumps(extract_ghactions.extract(tmp_path))
    assert PLANTED not in dumped
    assert "example.com" not in dumped          # environment url
    assert "inventory/hosts" not in dumped      # ansible-playbook option values
    assert "pw=" not in dumped and "requests" not in dumped


def test_run_lines_point_at_other_passes_ids(tmp_path):
    _repo(tmp_path)
    frag = extract_ghactions.extract(tmp_path)
    n = _nodes(frag)
    t = _triples(frag)
    j = "ghjob:.github/workflows/ci.yml#"
    lint, build, infra = j + "lint", j + "build", j + "infra"
    assert (lint, "runs_script", "module:bin/lint.sh") in t            # bare relative path
    assert (lint, "runs_script", "module:scripts/check.sh") in t       # bash <file>
    assert (lint, "runs_script", "module:tools/gen.py") in t           # python3 -u <file>
    assert (lint, "runs_script", "module:tools/report.py") in t        # uv run --with x <file>
    # working-directory + backslash continuation + an expression holding && and ||
    assert (build, "runs_script", "dockerfile:deploy/docker/web.Dockerfile") in t
    assert (build, "runs_script", "dockerfile:Dockerfile") in t        # `docker build ..` context
    assert (build, "runs_script", "module:deploy/run.sh") in t
    assert (infra, "runs_script", "module:infra/main.tf") in t
    assert (infra, "runs_script", "module:infra/vars.tf") in t
    assert not any(x[2].endswith("notes.txt") for x in t)
    assert (infra, "runs_script", "module:packer/image.pkr.hcl") in t  # cd, then packer build .
    assert (infra, "runs_script", "module:packer/vars.pkrvars.hcl") not in t
    assert (infra, "runs_script", "playbook:site.yml") in t
    assert (infra, "runs_script", "module:scripts/win.ps1") in t
    assert not any("missing" in x[2] for x in t)                       # no file, no edge
    reusable = "ghjob:.github/workflows/reusable.yml#go"
    assert (reusable, "runs_script", "module:bin/lint.sh") in t        # ${{ github.workspace }}
    # target nodes carry the attributes their owning pass writes, so the graphs join
    assert n["module:tools/gen.py"] == {"id": "module:tools/gen.py", "label": "tools/gen.py",
                                        "type": "module", "file_type": "python",
                                        "source_file": "tools/gen.py"}
    assert n["module:bin/lint.sh"]["file_type"] == "bash"
    ans = {x["id"]: x for x in extract_ansible.extract(tmp_path)["nodes"]}
    assert ans["playbook:site.yml"] == n["playbook:site.yml"]
    assert all(e["confidence"] == "INFERRED" for e in frag["edges"] if e["relation"] == "runs_script")


def test_shell_parsing_edge_cases(tmp_path):
    _mk(tmp_path, ".github/workflows/x.yaml", """\
on: [push, pull_request]
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - run: |
          cat <<EOF > out.txt
          ./bin/not-a-command.sh
          EOF
          # ./bin/commented.sh
          FOO=1 sudo -E ./bin/real.sh arg
          sh -c "cd sub && ./inner"
          ./bin/tool
          pwsh -Command "& ./scripts/a.ps1"
          if [ -f x ]; then ./bin/then.sh; fi
          ../escape.sh
          /usr/bin/env true
          "$HOME/x.sh"
""")
    for rel in ("bin/not-a-command.sh", "bin/commented.sh", "bin/real.sh", "sub/inner",
                "bin/tool", "scripts/a.ps1", "bin/then.sh"):
        _mk(tmp_path, rel, "x\n")
    _mk(tmp_path.parent, "escape.sh", "x\n")
    frag = extract_ghactions.extract(tmp_path)
    t = {(r, d) for _, r, d in _triples(frag)}
    assert _nodes(frag)["ghworkflow:.github/workflows/x.yaml"]["triggers"] == ["push", "pull_request"]
    assert ("runs_script", "module:bin/real.sh") in t
    assert ("runs_script", "file:sub/inner") in t           # extensionless: own node
    assert ("runs_script", "file:bin/tool") in t
    assert ("runs_script", "module:scripts/a.ps1") in t
    assert ("runs_script", "module:bin/then.sh") in t
    assert ("runs_script", "module:bin/not-a-command.sh") not in t   # heredoc body
    assert ("runs_script", "module:bin/commented.sh") not in t
    assert not any("escape" in d for _, d in t)
    # one shape for file:<rel> across passes (docker, ansible), so a merge never flips it
    tool = _nodes(frag)["file:bin/tool"]
    assert (tool["type"], tool["file_type"], tool["label"]) == ("file", "file", "tool")


def test_packer_recursive_walks_template_dirs(tmp_path):
    _mk(tmp_path, ".github/workflows/p.yml",
        "on: push\njobs:\n  fmt:\n    runs-on: x\n    steps:\n"
        "      - run: packer fmt -check -recursive images\n"
        "      - run: packer validate images/one\n")
    _mk(tmp_path, "images/one/main.pkr.hcl", "x\n")
    _mk(tmp_path, "images/two/main.pkr.json", "{}\n")
    _mk(tmp_path, "images/node_modules/skip.pkr.hcl", "x\n")
    _mk(tmp_path, "images/README.md", "x\n")
    frag = extract_ghactions.extract(tmp_path)
    got = sorted(e["target"] for e in frag["edges"] if e["relation"] == "runs_script")
    assert got == ["file:images/two/main.pkr.json", "module:images/one/main.pkr.hcl"]


def test_disabled_missing_and_malformed(tmp_path):
    assert extract_ghactions.extract(tmp_path) == {"nodes": [], "edges": []}
    _repo(tmp_path)
    assert extract_ghactions.extract(tmp_path, exclude=frozenset({".github"})) == \
        {"nodes": [], "edges": []}
    _mk(tmp_path, ".github/workflows/bad.yml", "jobs: [unclosed\n  - : :\n")
    _mk(tmp_path, ".github/workflows/list.yml", "- just\n- a list\n")
    _mk(tmp_path, ".github/workflows/jobs-list.yml", "on: push\njobs: [a, b]\n")
    _mk(tmp_path, ".github/workflows/weird.yml",
        "on: push\njobs:\n  a: 3\n  b:\n    steps: [1, {run: 5}, {uses: 7}]\n    needs: {x: 1}\n")
    _mk(tmp_path, ".github/actions/broken/action.yml", "runs: [\n")
    frag = extract_ghactions.extract(tmp_path)
    ids = _nodes(frag)
    assert "ghworkflow:.github/workflows/bad.yml" not in ids
    assert "ghworkflow:.github/workflows/list.yml" not in ids
    assert "ghworkflow:.github/workflows/jobs-list.yml" in ids
    assert "ghjob:.github/workflows/weird.yml#b" in ids
    assert "ghjob:.github/workflows/weird.yml#a" not in ids
    assert "ghaction:.github/actions/broken" not in ids
    assert "ghworkflow:.github/workflows/ci.yml" in ids  # the rest of the repo still graphs


def test_oversized_file_skipped(tmp_path, monkeypatch):
    _mk(tmp_path, ".github/workflows/big.yml", "on: push\njobs:\n  a:\n    runs-on: x\n")
    monkeypatch.setattr(extract_ghactions, "_MAX_BYTES", 10)
    assert extract_ghactions.extract(tmp_path)["nodes"] == []


def test_node_budget_caps_one_file(tmp_path, monkeypatch):
    jobs = "".join(f"  j{i}:\n    runs-on: x\n" for i in range(50))
    _mk(tmp_path, ".github/workflows/a.yml", "on: push\njobs:\n" + jobs)
    _mk(tmp_path, ".github/workflows/b.yml", "on: push\njobs:\n  only:\n    runs-on: x\n")
    monkeypatch.setattr(extract_ghactions, "_MAX_NODES", 5)
    ids = _nodes(extract_ghactions.extract(tmp_path))
    assert sum(i.startswith("ghjob:.github/workflows/a.yml") for i in ids) == 4
    assert "ghjob:.github/workflows/b.yml#only" in ids  # budget is per file


def test_alias_bomb_is_bounded(tmp_path):
    bomb = "a: &a [x, x, x, x, x, x, x, x, x]\n" + "".join(
        f"{c}: &{c} [*{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}]\n"
        for p, c in zip("abcdefgh", "bcdefghi"))
    _mk(tmp_path, ".github/workflows/bomb.yml",
        bomb + "on: push\njobs:\n  a:\n    runs-on: x\n    env: {X: *i}\n")
    frag = extract_ghactions.extract(tmp_path)
    assert "ghjob:.github/workflows/bomb.yml#a" in _nodes(frag)


def test_recursive_runs_on_alias_is_bounded(tmp_path):
    # PyYAML builds a mapping that contains itself; `labels` must never be followed into it
    _mk(tmp_path, ".github/workflows/ci.yml",
        "on: push\njobs:\n  a:\n    runs-on: &r {group: g, labels: *r}\n    steps: []\n")
    _mk(tmp_path, ".github/workflows/ok.yml", "on: push\njobs:\n  b:\n    runs-on: x\n")
    nodes = _nodes(extract_ghactions.extract(tmp_path))
    assert nodes["ghjob:.github/workflows/ci.yml#a"]["runs_on"] == {"group": "g"}
    assert "ghjob:.github/workflows/ok.yml#b" in nodes


def test_an_unforeseen_error_costs_only_that_file(tmp_path, monkeypatch):
    _mk(tmp_path, ".github/workflows/a.yml", "on: push\njobs:\n  a:\n    runs-on: x\n")
    _mk(tmp_path, ".github/workflows/b.yml", "on: push\njobs:\n  b:\n    runs-on: x\n")
    real = extract_ghactions._Pass.workflow

    def flaky(self, rel):
        if rel.endswith("a.yml"):
            raise RuntimeError("unforeseen")
        return real(self, rel)
    monkeypatch.setattr(extract_ghactions._Pass, "workflow", flaky)
    assert "ghjob:.github/workflows/b.yml#b" in _nodes(extract_ghactions.extract(tmp_path))


def test_powershell_steps_keep_backslash_paths(tmp_path):
    # posix shlex would read `.\scripts\build.ps1` as `.scriptsbuild.ps1`
    _mk(tmp_path, "scripts/build.ps1", "Write-Host x\n")
    _mk(tmp_path, "scripts/run.sh", "echo x\n")
    _mk(tmp_path, "tools/check.ps1", "Write-Host x\n")
    _mk(tmp_path, ".github/workflows/win.yml",
        "on: push\ndefaults:\n  run:\n    shell: pwsh\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: .\\scripts\\build.ps1\n"
        "      - run: pwsh -File .\\scripts\\build.ps1 -Mode x\n"
        "      - run: ./scripts/run.sh\n        shell: bash\n"
        "  c:\n    runs-on: ubuntu-latest\n    defaults:\n      run:\n        shell: bash\n"
        "    steps:\n      - run: .\\tools\\check.ps1\n")
    # no shell anywhere: a Windows runner defaults to pwsh, any other runner to bash
    _mk(tmp_path, ".github/workflows/default.yml",
        "on: push\njobs:\n  b:\n    runs-on: [self-hosted, windows]\n    steps:\n"
        "      - run: .\\tools\\check.ps1\n"
        "  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: .\\tools\\check.ps1\n")
    got = {(e["source"], e["target"]) for e in extract_ghactions.extract(tmp_path)["edges"]
           if e["relation"] == "runs_script"}
    wf, df = "ghjob:.github/workflows/win.yml#", "ghjob:.github/workflows/default.yml#"
    assert got == {(wf + "a", "module:scripts/build.ps1"), (wf + "a", "module:scripts/run.sh"),
                   (df + "b", "module:tools/check.ps1")}


def test_aliased_steps_cost_bounded_time(tmp_path):
    # 200 jobs share one list of 1000 aliased steps: a few kilobytes, 200k step visits
    import time
    _mk(tmp_path, "a.sh", "echo\n")
    y = ("on: push\nx-s: &s {run: ./a.sh x}\nx-steps: &ss [" + ", ".join(["*s"] * 1000) + "]\n"
         "jobs:\n  j0: &j {runs-on: x, steps: *ss}\n" + "".join(f"  j{i}: *j\n" for i in range(1, 200)))
    _mk(tmp_path, ".github/workflows/ci.yml", y)
    t = time.monotonic()
    frag = extract_ghactions.extract(tmp_path)
    assert time.monotonic() - t < 5
    assert ("ghjob:.github/workflows/ci.yml#j0", "runs_script", "module:a.sh") in _triples(frag)


def test_unclosed_expressions_cost_linear_time(tmp_path):
    # a lazy `\$\{\{(.*?)\}\}` regex is quadratic on unclosed `${{`
    import time
    _mk(tmp_path, ".github/workflows/ci.yml",
        "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: '" + "${{" * 70_000 + "'\n"
        "        working-directory: '" + "${{" * 70_000 + "'\n")
    t = time.monotonic()
    nodes = _nodes(extract_ghactions.extract(tmp_path))
    assert time.monotonic() - t < 5
    assert "ghjob:.github/workflows/ci.yml#a" in nodes


def test_deterministic(tmp_path):
    _repo(tmp_path)
    assert json.dumps(extract_ghactions.extract(tmp_path)) == \
        json.dumps(extract_ghactions.extract(tmp_path))


def _symlink(link, target, is_dir=False):
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")


def test_symlinks_out_of_root_are_not_read(tmp_path):
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    _mk(outside, "evil.yml", f"name: {PLANTED}\non: push\njobs:\n  a:\n    runs-on: x\n")
    _mk(outside, "evil.sh", "x\n")
    _mk(outside, "act/action.yml", f"name: {PLANTED}\nruns:\n  using: composite\n  steps: []\n")
    _mk(root, ".github/workflows/ok.yml",
        "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n"
        "      - run: ./link.sh\n      - uses: ./linked-action\n")
    (root / ".github/actions").mkdir(parents=True)
    _symlink(root / ".github/workflows/evil.yml", outside / "evil.yml")
    _symlink(root / "link.sh", outside / "evil.sh")
    _symlink(root / "linked-action", outside / "act", is_dir=True)
    _symlink(root / ".github/actions/out", outside / "act", is_dir=True)
    frag = extract_ghactions.extract(root)
    dumped = json.dumps(frag)
    assert PLANTED not in dumped
    assert "evil" not in dumped and "link.sh" not in dumped and "linked-action" not in dumped
    assert "ghjob:.github/workflows/ok.yml#a" in _nodes(frag)


def test_github_dir_symlinked_out_of_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _mk(tmp_path, "elsewhere/workflows/ci.yml", "on: push\njobs:\n  a:\n    runs-on: x\n")
    _symlink(root / ".github", tmp_path / "elsewhere", is_dir=True)
    assert extract_ghactions.extract(root) == {"nodes": [], "edges": []}
