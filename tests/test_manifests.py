"""Dependency-manifest extractor: packages, project references and solution structure."""
import json
import os

import pytest

from gateway.codegraph import extract_manifests

SECRET = "s3cr3t-Kp7wR-planted-value"


def _mk(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _index(frag):
    return ({n["id"]: n for n in frag["nodes"]},
            {(e["source"], e["relation"], e["target"]): e for e in frag["edges"]})


def _dotnet(root):
    _mk(root, "App.sln", """
Microsoft Visual Studio Solution File, Format Version 12.00
Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Web", "src\\Web\\Web.csproj", "{11111111-1111-1111-1111-111111111111}"
EndProject
Project("{2150E333-8FDC-42A3-9474-1A3956D46DE8}") = "tests", "tests", "{22222222-2222-2222-2222-222222222222}"
EndProject
Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Web.Tests", "tests\\Web.Tests\\Web.Tests.csproj", "{33333333-3333-3333-3333-333333333333}"
EndProject
Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Gone", "src\\Gone\\Gone.csproj", "{44444444-4444-4444-4444-444444444444}"
EndProject
""")
    _mk(root, "src/Web/Web.csproj", f"""<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <UserSecretsId>{SECRET}</UserSecretsId>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Example.Logging" Version="1.2.3" />
    <PackageReference Include="Example.Tools">
      <Version>4.5.0</Version>
      <PrivateAssets>all</PrivateAssets>
    </PackageReference>
    <PackageReference Include="Example.Central" />
  </ItemGroup>
</Project>
""")
    _mk(root, "src/Web/Program.cs", "class Program {}\n")
    _mk(root, "src/Web/Controllers/HomeController.cs", "class HomeController {}\n")
    _mk(root, "src/Web/obj/Debug/Generated.cs", "class Generated {}\n")
    _mk(root, "src/Web/bin/Release/Copied.cs", "class Copied {}\n")
    _mk(root, "src/Web/Nested/Nested.csproj", "<Project Sdk=\"Microsoft.NET.Sdk\" />\n")
    _mk(root, "src/Web/Nested/Lib.cs", "class Lib {}\n")
    _mk(root, "tests/Web.Tests/Web.Tests.csproj", """<?xml version="1.0" encoding="utf-8"?>
<Project ToolsVersion="15.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup>
    <TargetFrameworks>net8.0;net48</TargetFrameworks>
    <OutputType>Exe</OutputType>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="example.logging" Version="1.2.3" />
    <ProjectReference Include="..\\..\\src\\Web\\Web.csproj" />
    <ProjectReference Include="..\\..\\..\\outside\\Other.csproj" />
    <Compile Include="Tests.cs" />
    <Compile Include="**\\*.cs" />
  </ItemGroup>
</Project>
""")
    _mk(root, "tests/Web.Tests/Tests.cs", "class Tests {}\n")
    _mk(root, "tests/Web.Tests/Unlisted.cs", "class Unlisted {}\n")


def test_dotnet_projects_packages_and_sources(tmp_path):
    _dotnet(tmp_path)
    nodes, edges = _index(extract_manifests.extract(tmp_path))
    web, tests = "project:src/Web/Web.csproj", "project:tests/Web.Tests/Web.Tests.csproj"

    assert ("solution:App.sln", "contains", web) in edges
    assert ("solution:App.sln", "contains", tests) in edges
    assert not any(t in ("project:tests", "project:src/Gone/Gone.csproj")
                   for (_, _, t) in edges)       # solution folder; missing file

    assert nodes[web]["sdk"] == "Microsoft.NET.Sdk.Web"
    assert nodes[web]["target_frameworks"] == ["net8.0"]
    assert nodes[tests]["target_frameworks"] == ["net8.0", "net48"]
    assert nodes[tests]["output_type"] == "Exe"
    assert nodes[web]["label"] == "Web" and nodes[web]["type"] == "dotnet_project"

    assert edges[(web, "depends_on", "extpackage:nuget:example.logging")]["version"] == "1.2.3"
    tools = edges[(web, "depends_on", "extpackage:nuget:example.tools")]
    assert tools["version"] == "4.5.0" and tools["private"] is True
    assert "version" not in edges[(web, "depends_on", "extpackage:nuget:example.central")]
    # NuGet ids are case-insensitive: both projects share one package node
    assert (tests, "depends_on", "extpackage:nuget:example.logging") in edges
    assert nodes["extpackage:nuget:example.logging"]["label"] == "Example.Logging"

    assert (tests, "references_project", web) in edges
    assert not any("outside" in t for (_, _, t) in edges)

    # SDK default glob: the nearest project owns a file; bin/ and obj/ are build output
    c = edges[(web, "contains", "module:src/Web/Program.cs")]
    assert c["confidence"] == "INFERRED"
    assert (web, "contains", "module:src/Web/Controllers/HomeController.cs") in edges
    assert ("project:src/Web/Nested/Nested.csproj", "contains", "module:src/Web/Nested/Lib.cs") in edges
    assert (web, "contains", "module:src/Web/Nested/Lib.cs") not in edges
    assert not any("Generated" in t or "Copied" in t for (_, _, t) in edges)
    assert nodes["module:src/Web/Program.cs"]["file_type"] == "csharp"
    # default items off: only the explicit, literal Compile entries, EXTRACTED
    assert edges[(tests, "contains", "module:tests/Web.Tests/Tests.cs")]["confidence"] == "EXTRACTED"
    assert (tests, "contains", "module:tests/Web.Tests/Unlisted.cs") not in edges


def test_slnx(tmp_path):
    _mk(tmp_path, "lib/Lib.csproj", "<Project Sdk=\"Microsoft.NET.Sdk\" />\n")
    _mk(tmp_path, "App.slnx", "<Solution><Folder Name=\"/src/\"><Project Path=\"lib/Lib.csproj\" />"
                              "</Folder></Solution>\n")
    _, edges = _index(extract_manifests.extract(tmp_path))
    assert ("solution:App.slnx", "contains", "project:lib/Lib.csproj") in edges


def test_pyproject(tmp_path):
    _mk(tmp_path, "libs/core/pyproject.toml", "[project]\nname = 'core'\n")
    _mk(tmp_path, "pyproject.toml", f"""
[build-system]
requires = ["setuptools>=68", "wheel"]

[project]
name = "app"
dependencies = [
    "Web_Framework.Core[async, extra]>=1.0,<2",
    "plainpkg",
    "marker-pkg (>=2.0) ; python_version < '3.12'",
    "private-pkg @ https://user:{SECRET}@example.com/private-pkg.tar.gz",
]

[project.optional-dependencies]
docs = ["doc-tool==3.1"]

[dependency-groups]
test = ["test-runner>=8", {{include-group = "lint"}}]
lint = ["linter"]

[tool.poetry.dependencies]
python = "^3.11"
poetry-pkg = "^1.4"
git-pkg = {{ git = "https://{SECRET}@example.com/git-pkg.git", branch = "main" }}
opt-pkg = {{ version = "2.0", optional = true }}
core = {{ path = "libs/core", develop = true }}

[tool.poetry.group.dev.dependencies]
dev-tool = "*"
""")
    nodes, edges = _index(extract_manifests.extract(tmp_path))
    m = "manifest:pyproject.toml"
    assert nodes[m]["type"] == "pyproject" and nodes[m]["package_name"] == "app"

    e = edges[(m, "depends_on", "extpackage:pypi:web-framework-core")]
    assert e["specifier"] == ">=1.0,<2" and e["extras"] == ["async", "extra"] and e["scope"] == "main"
    assert nodes["extpackage:pypi:web-framework-core"]["ecosystem"] == "pypi"
    assert "specifier" not in edges[(m, "depends_on", "extpackage:pypi:plainpkg")]
    assert edges[(m, "depends_on", "extpackage:pypi:marker-pkg")]["specifier"] == ">=2.0"
    assert edges[(m, "depends_on", "extpackage:pypi:private-pkg")]["origin"] == "direct_reference"
    d = edges[(m, "depends_on", "extpackage:pypi:doc-tool")]
    assert d["scope"] == "optional" and d["group"] == "docs"
    t = edges[(m, "depends_on", "extpackage:pypi:test-runner")]
    assert t["scope"] == "group" and t["group"] == "test"
    assert edges[(m, "depends_on", "extpackage:pypi:setuptools")]["scope"] == "build"

    assert edges[(m, "depends_on", "extpackage:pypi:poetry-pkg")]["specifier"] == "^1.4"
    assert edges[(m, "depends_on", "extpackage:pypi:git-pkg")]["origin"] == "git"
    assert edges[(m, "depends_on", "extpackage:pypi:opt-pkg")]["optional"] is True
    assert edges[(m, "depends_on", "extpackage:pypi:dev-tool")]["group"] == "dev"
    assert (m, "depends_on", "manifest:libs/core/pyproject.toml") in edges
    assert "extpackage:pypi:python" not in nodes and "extpackage:pypi:core" not in nodes


def test_requirements(tmp_path):
    _mk(tmp_path, "requirements/base.txt", "shared-lib==1.0\n")
    _mk(tmp_path, "constraints.txt", "shared-lib<2\n")
    _mk(tmp_path, "requirements-dev.txt", f"""# development requirements
-r requirements/base.txt
--constraint constraints.txt
--index-url https://user:{SECRET}@example.com/simple
--extra-index-url=https://{SECRET}@example.com/extra
-e git+https://{SECRET}@example.com/repo.git#egg=editable
Hashed_Pkg==2.0 \\
    --hash=sha256:{"cd" * 32}
named-url @ https://{SECRET}@example.com/named.whl
https://example.com/{SECRET}/anon-1.0.tar.gz
./local/{SECRET}
local-archive-{SECRET}.whl
spaced >= 1.0, != 1.5  # trailing comment
""")
    nodes, edges = _index(extract_manifests.extract(tmp_path))
    m = "manifest:requirements-dev.txt"
    assert ("manifest:requirements/base.txt", "depends_on", "extpackage:pypi:shared-lib") in edges
    assert (m, "includes", "manifest:requirements/base.txt") in edges
    assert (m, "constrained_by", "manifest:constraints.txt") in edges
    assert edges[(m, "depends_on", "extpackage:pypi:hashed-pkg")]["specifier"] == "==2.0"
    assert edges[(m, "depends_on", "extpackage:pypi:named-url")]["origin"] == "direct_reference"
    assert edges[(m, "depends_on", "extpackage:pypi:spaced")]["specifier"] == ">= 1.0, != 1.5"
    pkgs = {t for (s, r, t) in edges if s == m and r == "depends_on"}
    assert pkgs == {"extpackage:pypi:hashed-pkg", "extpackage:pypi:named-url", "extpackage:pypi:spaced"}


def test_package_json(tmp_path):
    _mk(tmp_path, "web/package.json", json.dumps({
        "name": "@example/web",
        "version": "1.0.0",
        "dependencies": {"ui-kit": "^4.2.0", "@scope/util": "~1.0.1",
                         "from-git": f"git+https://user:{SECRET}@example.com/x.git",
                         "local-lib": "file:../lib", "ws-lib": "workspace:*"},
        "devDependencies": {"test-runner": ">=29 <30", "latest-pkg": "latest"},
        "peerDependencies": {"peer-lib": "*"},
        "scripts": {"build": f"TOKEN={SECRET} build"},
    }))
    nodes, edges = _index(extract_manifests.extract(tmp_path))
    m = "manifest:web/package.json"
    assert nodes[m]["package_name"] == "@example/web" and nodes[m]["type"] == "package_json"
    assert edges[(m, "depends_on", "extpackage:npm:ui-kit")]["version"] == "^4.2.0"
    assert edges[(m, "depends_on", "extpackage:npm:@scope/util")]["scope"] == "main"
    assert edges[(m, "depends_on", "extpackage:npm:from-git")]["origin"] == "url"
    assert edges[(m, "depends_on", "extpackage:npm:local-lib")]["origin"] == "local"
    assert edges[(m, "depends_on", "extpackage:npm:ws-lib")]["origin"] == "workspace"
    t = edges[(m, "depends_on", "extpackage:npm:test-runner")]
    assert t["scope"] == "dev" and t["version"] == ">=29 <30"
    assert edges[(m, "depends_on", "extpackage:npm:peer-lib")]["scope"] == "peer"


def test_galaxy_requirements(tmp_path):
    _mk(tmp_path, "requirements.yml", f"""---
collections:
  - example.monitoring
  - name: example.network
    version: ">=2.0.0"
  - name: https://user:{SECRET}@example.com/collections/example-tools.git
    type: git
roles:
  - name: web_server
    src: https://user:{SECRET}@example.com/roles/web-server.git
    version: v1.2
  - src: example.database
""")
    _mk(tmp_path, "roles/requirements.yml", "- src: example.legacy\n- example.other\n")
    _mk(tmp_path, "tools/requirements.yaml", "settings:\n  flag: true\n")
    nodes, edges = _index(extract_manifests.extract(tmp_path))
    m = "manifest:requirements.yml"
    assert edges[(m, "depends_on", "extpackage:galaxy:example.monitoring")]["kind"] == "collection"
    assert edges[(m, "depends_on", "extpackage:galaxy:example.network")]["version"] == ">=2.0.0"
    assert edges[(m, "depends_on", "extpackage:galaxy:example-tools")]["origin"] == "scm"
    r = edges[(m, "depends_on", "extpackage:galaxy:web_server")]
    assert r["kind"] == "role" and r["version"] == "v1.2"
    assert (m, "depends_on", "extpackage:galaxy:example.database") in edges
    assert ("manifest:roles/requirements.yml", "depends_on", "extpackage:galaxy:example.legacy") in edges
    assert ("manifest:roles/requirements.yml", "depends_on", "extpackage:galaxy:example.other") in edges
    assert "manifest:tools/requirements.yaml" not in nodes     # some other tool's file


def test_node_for_identity():
    nf = extract_manifests.node_for
    assert nf("a/B.csproj")["id"] == "project:a/B.csproj"
    assert nf("X.sln")["id"] == "solution:X.sln"
    assert nf("pyproject.toml")["id"] == "manifest:pyproject.toml"
    assert nf("requirements/prod.txt")["type"] == "requirements"
    assert nf("notes.txt") is None and nf("main.py") is None


def test_secret_values_never_reach_the_graph(tmp_path):
    _dotnet(tmp_path)        # carries a UserSecretsId
    _mk(tmp_path, "py/pyproject.toml", f"""[project]
name = "svc"
dependencies = ["private-pkg @ https://user:{SECRET}@example.com/p.tar.gz"]
[tool.poetry.dependencies]
git-pkg = {{ git = "https://{SECRET}@example.com/g.git" }}
url-pkg = {{ url = "https://example.com/{SECRET}.whl" }}
[tool.custom]
token = "{SECRET}"
""")
    _mk(tmp_path, "py/requirements.txt", f"--index-url https://user:{SECRET}@example.com/simple\n"
                                         f"-e git+https://{SECRET}@example.com/r.git#egg=r\n"
                                         f"named @ https://{SECRET}@example.com/n.whl\nok-pkg==1\n")
    _mk(tmp_path, "js/package.json", json.dumps({
        "name": "js", "dependencies": {"g": f"git+https://u:{SECRET}@example.com/g.git",
                                       "t": f"https://example.com/{SECRET}.tgz"},
        "config": {"token": SECRET}}))
    _mk(tmp_path, "requirements.yml", f"""collections:
  - name: https://u:{SECRET}@example.com/c/coll.git
    type: git
    version: "{SECRET}/../x"
roles:
  - src: git+https://u:{SECRET}@example.com/r/role.git,v1,role_name
""")
    res = extract_manifests.extract(tmp_path)
    nodes, edges = _index(res)
    assert "extpackage:pypi:ok-pkg" in nodes and "extpackage:galaxy:role_name" in nodes
    assert SECRET not in json.dumps(res)


def test_malformed_unreadable_and_linked_files_are_skipped(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _mk(outside, "pyproject.toml", "[project]\ndependencies = ['leaked-pkg']\n")
    _mk(repo, "bad/pyproject.toml", "[project\ndependencies = [\n")
    _mk(repo, "bad/package.json", "{not json")
    _mk(repo, "bad/App.csproj", "<Project><ItemGroup>")
    _mk(repo, "bad/requirements.yml", "collections: [unclosed\n")
    _mk(repo, "dtd/App.csproj", "<!DOCTYPE p [<!ENTITY e \"leaked-entity\">]>"
                                "<Project><ItemGroup><PackageReference Include=\"&e;\" /></ItemGroup></Project>")
    _mk(repo, "node_modules/dep/package.json", json.dumps({"dependencies": {"vendored-pkg": "1"}}))
    _mk(repo, "big/requirements.txt", "big-pkg==1\n" + "# pad\n" * 300)
    _mk(repo, "good/requirements.txt", "good-pkg==1\n")
    try:
        os.symlink(outside / "pyproject.toml", repo / "pyproject.toml")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(extract_manifests, "MAX_BYTES", 600)

    res = extract_manifests.extract(repo)
    blob = json.dumps(res)
    for absent in ("leaked", "vendored", "big-pkg"):
        assert absent not in blob
    assert "extpackage:pypi:good-pkg" in blob
    assert json.dumps(extract_manifests.extract(repo)) == blob


def test_blank_runs_in_a_solution_cost_linear_time(tmp_path):
    # `^\s*` under re.M re-consumes every following newline from each line start (quadratic)
    import time
    _mk(tmp_path, "lib/Lib.csproj", "<Project></Project>\n")
    _mk(tmp_path, "App.sln", "\n" * 400_000 + 'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = '
                             '"Lib", "lib\\Lib.csproj", "{11111111-1111-1111-1111-111111111111}"\nEndProject\n')
    t = time.monotonic()
    _, edges = _index(extract_manifests.extract(tmp_path))
    assert time.monotonic() - t < 5
    assert ("solution:App.sln", "contains", "project:lib/Lib.csproj") in edges
