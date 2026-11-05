"""Azure Bicep extractor: declarations, the references between them, and who deploys what."""
import json
import os

import pytest

from gateway.codegraph import extract_bicep

# Unique values planted in the fixture. None of them may reach the graph: they stand for the
# defaults, assignments and inline values a template carries next to its structure.
PLANTED = ("PLANTED-default-zq81", "PLANTED-var-q77", "PLANTED-param-k42", "PLANTED-cli-m55",
           "PLANTED-desc-w19", "PLANTED-prop-h63", "00000000-1111-2222-3333-444444444444",
           "rg-planted-group")

MAIN = """\
targetScope = 'resourceGroup'

@description('PLANTED-desc-w19 resource fake \\'Microsoft.Fake/inDescription@2020-01-01\\' = {}')
param location string = resourceGroup().location

@secure()
param adminPassword string

param appName string = 'PLANTED-default-zq81'

param deployExtra bool = false

var settings = {
  apiKey: 'PLANTED-var-q77'
  storeId: store.id
}

// resource commented 'Microsoft.Fake/inComment@2020-01-01' = {}
/* module blockcomment './nowhere.bicep' = {} */

resource store 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: 'st${appName}'
  location: location
  properties: {
    note: '''
resource notreal 'Microsoft.Fake/inMultiline@2020-01-01' = {
'''
    label: 'PLANTED-prop-h63 { [ ('
  }

  resource blobs 'blobServices' = {
    name: 'default'
  }
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  name: 'data'
  parent: store::blobs
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: 'kv-app'
}

resource web 'Microsoft.Web/sites@2023-12-01' = {
  name: appName
  location: location
  properties: {
    serverFarmId: plan.outputs.planId
    siteConfig: {
      appSettings: [
        { name: 'DB_PASSWORD', value: '@Microsoft.KeyVault(VaultName=${vault.name};SecretName=db-password)' }
        { name: 'API_KEY', value: '@Microsoft.KeyVault(VaultName=kv-app;SecretName=api-key)' }
        { name: 'TOKEN', value: '@Microsoft.KeyVault(SecretUri=https://kv-other.vault.azure.net/secrets/token/)' }
      ]
    }
  }
  dependsOn: [
    container
  ]
}

module plan '../modules/plan.bicep' = {
  name: 'plan'
  params: {
    location: location
    settings: settings
  }
}

module registry 'br/public:avm/res/web/serverfarm:0.1.0' = if (deployExtra) {
  name: 'registry'
  params: {
    adminPassword: vault.getSecret('admin-password')
    container: deployExtra
  }
}

output webId string = web.id
output store string = store.name
"""

PLAN = """\
param location string
param settings object

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'plan'
  location: location
}

output planId string = plan.id
"""

PARAMS = """\
using '../app/main.bicep'

param appName = 'PLANTED-param-k42'
param adminPassword = az.getSecret('00000000-1111-2222-3333-444444444444', 'rg-planted-group', 'kv-app', 'admin-password')
param notInTemplate = 'x'
"""

DEPLOY_SH = """\
#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
az deployment group create \\
  --resource-group rg-planted-group \\
  --template-file "$SCRIPT_DIR/main.bicep" \\
  --parameters "$SCRIPT_DIR/../env/prod.bicepparam" adminPassword="PLANTED-cli-m55"
"""

WHATIF_SH = """\
#!/bin/bash
az deployment group what-if \\
    --resource-group rg-planted-group \\
    --parameters "$(dirname "$0")/../env/prod.bicepparam"
"""

DEPLOY_PS1 = """\
New-AzResourceGroupDeployment -ResourceGroupName rg-planted-group `
  -TemplateFile (Join-Path $PSScriptRoot 'main.bicep') `
  -TemplateParameterFile "$PSScriptRoot\\..\\env\\prod.bicepparam" -WhatIf
"""

F = "app/main.bicep"


def _mk(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _repo(root):
    _mk(root, F, MAIN)
    _mk(root, "modules/plan.bicep", PLAN)
    _mk(root, "env/prod.bicepparam", PARAMS)
    _mk(root, "app/deploy.sh", DEPLOY_SH)
    _mk(root, "app/whatif.sh", WHATIF_SH)
    _mk(root, "app/deploy.ps1", DEPLOY_PS1)


def _index(frag):
    return ({n["id"]: n for n in frag["nodes"]},
            {(e["source"], e["relation"], e["target"]): e for e in frag["edges"]})


def test_files_resources_modules_params_outputs(tmp_path):
    _repo(tmp_path)
    nodes, edges = _index(extract_bicep.extract(tmp_path))

    assert nodes[f"bicep:{F}"]["type"] == "bicep_template"
    assert nodes[f"bicep:{F}"]["target_scope"] == "resourceGroup"
    assert nodes["bicep:env/prod.bicepparam"]["type"] == "bicep_params"

    store = nodes[f"bicepresource:{F}#store"]
    assert store["resource_type"] == "Microsoft.Storage/storageAccounts"
    assert store["api_version"] == "2023-01-01"
    assert store["existing"] is False and store["source_file"] == F
    assert nodes[f"bicepresource:{F}#vault"]["existing"] is True

    for kind, name in (("bicepresource", "web"), ("bicepmodule", "plan"),
                       ("bicepparam", "location"), ("bicepoutput", "webId")):
        assert (f"bicep:{F}", "declares", f"{kind}:{F}#{name}") in edges

    assert nodes[f"bicepparam:{F}#adminPassword"]["secure"] is True
    assert nodes[f"bicepparam:{F}#appName"]["secure"] is False
    assert nodes[f"bicepparam:{F}#appName"]["param_type"] == "string"
    assert nodes[f"bicepoutput:{F}#webId"]["output_type"] == "string"


def test_nested_resource_inherits_type_and_is_a_child(tmp_path):
    _repo(tmp_path)
    nodes, edges = _index(extract_bicep.extract(tmp_path))
    blobs = nodes[f"bicepresource:{F}#store::blobs"]
    assert blobs["resource_type"] == "Microsoft.Storage/storageAccounts/blobServices"
    assert blobs["api_version"] == "2023-01-01"
    assert (f"bicepresource:{F}#store::blobs", "child_of", f"bicepresource:{F}#store") in edges
    # `parent:` may name a nested resource through the `::` accessor
    assert (f"bicepresource:{F}#container", "child_of", f"bicepresource:{F}#store::blobs") in edges
    # being a child is not also an inferred dependency on the parent
    assert (f"bicepresource:{F}#store::blobs", "depends_on", f"bicepresource:{F}#store") not in edges
    # and a parent does not depend on the children declared inside its body
    assert not [k for k in edges if k[0] == f"bicepresource:{F}#store" and k[1] == "depends_on"]


def test_strings_and_comments_declare_nothing(tmp_path):
    _repo(tmp_path)
    nodes, _ = _index(extract_bicep.extract(tmp_path))
    for word in ("fake", "commented", "blockcomment", "notreal", "nowhere"):
        assert not [n for n in nodes if word in n.lower()], word
    resources = {n for n in nodes if n.startswith(f"bicepresource:{F}#")}
    assert resources == {f"bicepresource:{F}#{s}"
                         for s in ("store", "store::blobs", "container", "vault", "web")}


def test_modules_resolve_locally_or_to_the_registry(tmp_path):
    _repo(tmp_path)
    nodes, edges = _index(extract_bicep.extract(tmp_path))
    assert (f"bicepmodule:{F}#plan", "uses_module", "bicep:modules/plan.bicep") in edges
    ext = "extbicep:br/public:avm/res/web/serverfarm:0.1.0"
    assert (f"bicepmodule:{F}#registry", "uses_module", ext) in edges
    assert nodes[f"bicepmodule:{F}#registry"]["conditional"] is True
    # the module's own file is graphed too, so the edge lands on a real node
    assert "bicep:modules/plan.bicep" in nodes
    assert "bicepresource:modules/plan.bicep#plan" in nodes


def test_explicit_and_inferred_dependencies(tmp_path):
    _repo(tmp_path)
    _, edges = _index(extract_bicep.extract(tmp_path))
    r = lambda s: f"bicepresource:{F}#{s}"  # noqa: E731
    m = lambda s: f"bicepmodule:{F}#{s}"    # noqa: E731

    assert edges[(r("web"), "depends_on", r("container"))]["confidence"] == "EXTRACTED"
    # <sym>.outputs / interpolated <sym>.name are references, inferred
    assert edges[(r("web"), "depends_on", m("plan"))]["confidence"] == "INFERRED"
    assert edges[(r("web"), "depends_on", r("vault"))]["confidence"] == "INFERRED"
    # through a var: the module passes `settings`, and `settings` reads store.id
    assert edges[(m("plan"), "depends_on", r("store"))]["confidence"] == "INFERRED"
    assert (f"bicepoutput:{F}#webId", "depends_on", r("web")) in edges
    # an output may share a resource's name and still reference it
    assert (f"bicepoutput:{F}#store", "depends_on", r("store")) in edges
    # object keys that happen to match a symbol are not references
    assert (m("registry"), "depends_on", r("container")) not in edges


def test_secrets_are_read_by_name(tmp_path):
    _repo(tmp_path)
    nodes, edges = _index(extract_bicep.extract(tmp_path))
    web = f"bicepresource:{F}#web"
    assert (web, "reads_secret", "kvsecret:kv-app/api-key") in edges
    assert (web, "reads_secret", "kvsecret:kv-other/token") in edges
    # the vault is an interpolation here: the secret is still named, the vault is not guessed
    assert (web, "reads_secret", "kvsecret:db-password") in edges
    # vault.getSecret() names the vault through the existing resource's literal name
    assert (f"bicepmodule:{F}#registry", "reads_secret", "kvsecret:kv-app/admin-password") in edges
    # az.getSecret() in a parameter file: vault and secret are the 3rd and 4th arguments
    assert ("bicep:env/prod.bicepparam", "reads_secret", "kvsecret:kv-app/admin-password") in edges
    assert nodes["kvsecret:kv-app/api-key"]["vault"] == "kv-app"


def test_parameter_file_links_to_its_template(tmp_path):
    _repo(tmp_path)
    _, edges = _index(extract_bicep.extract(tmp_path))
    pf = "bicep:env/prod.bicepparam"
    assert (pf, "parameterizes", f"bicep:{F}") in edges
    assert (pf, "sets_param", f"bicepparam:{F}#appName") in edges
    assert (pf, "sets_param", f"bicepparam:{F}#adminPassword") in edges
    # a name the template does not declare gets no dangling edge
    assert not [k for k in edges if k[0] == pf and k[2].endswith("#notInTemplate")]


def test_deployment_scripts_deploy_template_and_parameters(tmp_path):
    _repo(tmp_path)
    nodes, edges = _index(extract_bicep.extract(tmp_path))
    pf = "bicep:env/prod.bicepparam"
    for script in ("app/deploy.sh", "app/whatif.sh", "app/deploy.ps1"):
        sid = f"module:{script}"
        assert (sid, "deploys", f"bicep:{F}") in edges, script
        assert (sid, "deploys", pf) in edges, script
        # same id and shape as the tree-sitter pass, so the two graphs join
        assert nodes[sid]["type"] == "module" and nodes[sid]["source_file"] == script
    assert edges[("module:app/deploy.sh", "deploys", pf)]["operations"] == ["create"]
    assert edges[("module:app/whatif.sh", "deploys", pf)]["operations"] == ["what-if"]
    assert edges[("module:app/deploy.ps1", "deploys", pf)]["operations"] == ["what-if"]
    assert nodes["module:app/deploy.ps1"]["file_type"] == "powershell"


def test_unresolvable_or_unrelated_scripts_add_nothing(tmp_path):
    _repo(tmp_path)
    _mk(tmp_path, "app/other.sh", "#!/bin/bash\necho deployment done\n")
    _mk(tmp_path, "app/unknown.sh",
        "#!/bin/bash\naz deployment group create -f \"$SOMEWHERE/main.bicep\"\n")
    _mk(tmp_path, "app/missing.sh", "az deployment sub create -f ./absent.bicep\n")
    nodes, _ = _index(extract_bicep.extract(tmp_path))
    for s in ("app/other.sh", "app/unknown.sh", "app/missing.sh"):
        assert f"module:{s}" not in nodes


def test_no_value_reaches_the_graph(tmp_path):
    _repo(tmp_path)
    dumped = json.dumps(extract_bicep.extract(tmp_path))
    for value in PLANTED:
        assert value not in dumped, value


def test_output_is_deterministic(tmp_path):
    _repo(tmp_path)
    assert extract_bicep.extract(tmp_path) == extract_bicep.extract(tmp_path)


def test_malformed_and_oversized_files_are_skipped(tmp_path, monkeypatch):
    _repo(tmp_path)
    _mk(tmp_path, "bad/unterminated.bicep", "resource x 'Microsoft.A/b@1' = {\n  name: 'open\n")
    _mk(tmp_path, "bad/brackets.bicep", "}}}]]) resource y 'Microsoft.A/b@1' = {{{{ '${'\n")
    (tmp_path / "bad/binary.bicep").write_bytes(b"\xff\xfe\x00resource\x00'\x00${")
    _mk(tmp_path, "bad/interp.bicepparam", "using '${\nparam = getSecret(\n")
    monkeypatch.setattr(extract_bicep, "MAX_BYTES", len(MAIN.encode()) - 1)
    nodes, edges = _index(extract_bicep.extract(tmp_path))
    assert f"bicep:{F}" not in nodes                       # over the cap: not read at all
    assert "bicep:modules/plan.bicep" in nodes             # the rest still builds
    assert all(isinstance(e, dict) for e in edges.values())


def test_pruned_directories_and_include_override(tmp_path):
    _mk(tmp_path, "node_modules/pkg/x.bicep", "param a string\n")
    _mk(tmp_path, ".hidden/y.bicep", "param b string\n")
    nodes, _ = _index(extract_bicep.extract(tmp_path))
    assert nodes == {}
    nodes, _ = _index(extract_bicep.extract(tmp_path, include=frozenset({".hidden"})))
    assert "bicepparam:.hidden/y.bicep#b" in nodes
    _mk(tmp_path, "gen/z.bicep", "param c string\n")
    nodes, _ = _index(extract_bicep.extract(tmp_path, exclude=frozenset({"gen"})))
    assert nodes == {}


def test_symlink_out_of_root_is_not_read(tmp_path):
    outside = tmp_path / "outside"
    _mk(outside, "secret.bicep", "param leaked string\n")
    root = tmp_path / "repo"
    _mk(root, "ok.bicep", "param fine string\n")
    try:
        os.symlink(outside / "secret.bicep", root / "linked.bicep")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    nodes, _ = _index(extract_bicep.extract(root))
    assert "bicepparam:ok.bicep#fine" in nodes
    assert not [n for n in nodes if "leaked" in n or "linked" in n]


def test_module_paths_outside_the_root_or_missing(tmp_path):
    # a monorepo subfolder graphed on its own: `../../shared` is not a registry module
    _mk(tmp_path, "infra/main.bicep",
        "module m '../../shared/net.bicep' = {\n  name: 'm'\n}\n"
        "module n 'missing.bicep' = {\n  name: 'n'\n}\n"
        "module r 'br/public:avm/res/x:1.0.0' = {\n  name: 'r'\n}\n")
    _mk(tmp_path, "infra/dev.bicepparam", "using '../../elsewhere/main.bicep'\n")
    _mk(tmp_path, "infra/prod.bicepparam", "using 'gone.bicep'\nextends 'base.bicepparam'\n")
    nodes, edges = _index(extract_bicep.extract(tmp_path))
    targets = {(s, t) for s, r, t in edges if r in ("uses_module", "parameterizes", "extends")}
    assert targets == {
        ("bicepmodule:infra/main.bicep#n", "bicep:infra/missing.bicep"),
        ("bicepmodule:infra/main.bicep#r", "extbicep:br/public:avm/res/x:1.0.0"),
        ("bicep:infra/prod.bicepparam", "bicep:infra/gone.bicep"),
        ("bicep:infra/prod.bicepparam", "bicep:infra/base.bicepparam")}
    assert not any(n.startswith("extbicep:..") for n in nodes)
    assert nodes["bicep:infra/missing.bicep"]["missing"] is True
    assert nodes["bicep:infra/missing.bicep"]["type"] == "bicep_template"
    assert nodes["bicep:infra/base.bicepparam"]["type"] == "bicep_params"
    assert all("type" in nodes[t] for _, t in targets)


def test_doubling_variables_in_a_deploy_script_stay_small(tmp_path):
    # `A{i}=$A{i-1}$A{i-1}` doubles the stored value per line: 40 lines would need terabytes
    import resource
    import time
    _mk(tmp_path, "main.bicep", "param p string\n")
    _mk(tmp_path, "deploy.sh", "# deployment\nA0=xx\n" + "".join(
        f"A{i}=$A{i - 1}$A{i - 1}\n" for i in range(1, 40))
        + "T=main.bicep\naz deployment group create -g rg -f $T $A39\n")
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t = time.monotonic()
    _, edges = _index(extract_bicep.extract(tmp_path))
    assert time.monotonic() - t < 5
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before < 200 * 1024   # KiB
    assert ("module:deploy.sh", "deploys", "bicep:main.bicep") in edges


@pytest.mark.parametrize("name,text", [
    ("main.bicep", "var x = " + "a" * 100_000 + "\n"),                                # receiver scan
    ("main.bicep", "resource p 'A/b@1' = {\n" + "".join(
        f"  resource c{i} 'x' = {{}}\n" for i in range(8000)) + "}\n"),              # nesting
    ("main.bicep", "var x = [\n" + "kv.getSecret('a'\n" * 20_000 + "]\n"),           # unbalanced
    ("deploy.ps1", "# deployment\n" + "<#" * 100_000),                                # block comment
])
def test_pathological_inputs_cost_linear_time(tmp_path, name, text):
    import time
    _mk(tmp_path, name, text)
    t = time.monotonic()
    extract_bicep.extract(tmp_path)
    assert time.monotonic() - t < 10


def test_precomputed_bracket_lookups_match_the_plain_scans():
    text = ("resource a 'x@1' = {\n  p: [1, (2), {q: 'a}(['}]\n  r: { s: x[0] }\n}\n"
            "var b = ((\n] } ) [ {\n")
    doc = extract_bicep._Doc(text)
    for i, ch in enumerate(doc.code):
        if ch in "{[(":
            assert doc.match(i) == extract_bicep._match(doc.code, i), i
        for j in range(i, len(doc.code) + 1, 3):
            assert doc.rel_depth(i, j) == extract_bicep._rel_depth(doc.code, i, j), (i, j)


def test_repo_without_bicep_is_empty(tmp_path):
    _mk(tmp_path, "src/app.py", "x = 1\n")
    _mk(tmp_path, "scripts/run.sh", "#!/bin/bash\necho hi\n")
    assert extract_bicep.extract(tmp_path) == {"nodes": [], "edges": []}
