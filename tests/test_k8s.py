"""Kubernetes / Helm extractor: charts, templates, values, helm scripts, manifests, kustomize."""
from __future__ import annotations

import json
import os

import pytest

from gateway.codegraph import extract_k8s

SECRET = "kgSecretCanary-7f3a91d2"


def _mk(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _rels(frag):
    return {(e["source"], e["relation"], e["target"]) for e in frag["edges"]}


def _node(frag, nid):
    return next(n for n in frag["nodes"] if n["id"] == nid)


def _ids(frag):
    return {n["id"] for n in frag["nodes"]}


def _helm_repo(root):
    """services/web/helm is installed by a release script; it depends on a local helper chart and an
    external one, and reads its values through Go templates that are not valid YAML."""
    _mk(root, "services/web/helm/Chart.yaml",
        "apiVersion: v2\nname: web\ntype: application\nversion: 0.2.0\nappVersion: 1.4.0\n"
        "dependencies:\n"
        "- name: helper\n  version: 0.1.0\n  repository: file://../../helper/helm\n  alias: helperJob\n"
        "- name: cache\n  version: 1.x.x\n"
        f"  repository: https://user:{SECRET}@charts.example.com/stable/?token={SECRET}\n")
    _mk(root, "services/web/helm/Chart.lock",
        "dependencies:\n- name: helper\n  repository: file://../../helper/helm\n  version: 0.1.0\n"
        "- name: cache\n  repository: https://charts.example.com/stable\n  version: 1.2.3\n"
        "digest: sha256:00\n")
    _mk(root, "services/web/helm/values.yaml",
        f"image:\n  repository:\n  tag:\nsecret:\n  stringData:\n    TOKEN: {SECRET}\n")
    _mk(root, "services/web/helm/templates/deployment.yaml",
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  labels:\n    app: web\n"
        "spec:\n  selector:\n    matchLabels:\n      app: web\n  template:\n    metadata:\n"
        "      annotations:\n        checksum/secret: {{ .Values.secret | toYaml | sha256sum }}\n"
        "      labels:\n        app: web\n    spec:\n"
        "      serviceAccountName: web\n"
        "      imagePullSecrets:\n      - name: registry\n"
        "      containers:\n      - name: web\n"
        "        image: {{ .Values.image.repository }}:{{ .Values.image.tag }}\n"
        "        {{- if .Values.resources }}\n"
        "        resources:\n          {{- toYaml .Values.resources | nindent 10 }}\n"
        "        {{- end }}\n"
        "        envFrom:\n        - secretRef:\n            name: web\n"
        "        env:\n        - name: MODE\n"
        f"          value: {SECRET}\n"
        "        - name: DB_HOST\n          valueFrom:\n            configMapKeyRef:\n"
        "              name: web-config\n              key: host\n"
        "---\n"
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ include \"web.fullname\" . }}\n"
        f"data:\n  mode: {SECRET}\n")
    _mk(root, "services/web/helm/templates/secret.yaml",
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: web\nstringData:\n"
        "{{ .Values.secret.stringData | toYaml | nindent 2 }}\n"
        f"  INLINE: {SECRET}\n")
    _mk(root, "services/web/helm/templates/service.yaml",
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: web\nspec:\n  selector:\n    app: web\n"
        "  ports:\n  - port: 80\n")
    _mk(root, "services/web/helm/templates/ingress.yaml",
        "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: web\nspec:\n"
        "  ingressClassName: {{ .Values.ingress.className }}\n  rules:\n"
        "  - host: {{ .Values.ingress.host }}\n    http:\n      paths:\n      - path: /\n"
        "        backend:\n          service:\n            name: web\n            port:\n"
        "              number: 80\n")
    _mk(root, "services/web/helm/templates/_helpers.tpl",
        "{{- define \"web.fullname\" -}}\n{{ .Release.Name }}-web\n{{- end }}\n")
    _mk(root, "services/web/values-base.yaml", "helperJob:\n  enabled: true\nimage:\n  tag: x\n")
    _mk(root, "services/web/values-prod.yaml", f"ingress:\n  host: {SECRET}.example.com\n")
    _mk(root, "services/web/deploy/release.sh",
        "#!/bin/sh\nset -eu\nhelm upgrade web ../helm \\\n  --install \\\n  -n web \\\n"
        "  --set image.tag=" + SECRET + " \\\n"
        "  -f ../values-prod.yaml \\\n  -f ../values-base.yaml \\\n  -f ../prod-secrets.yaml\n")
    # the helper chart: RBAC wiring plus a job using the service account
    _mk(root, "services/helper/helm/Chart.yaml", "apiVersion: v2\nname: helper\nversion: 0.1.0\n")
    _mk(root, "services/helper/helm/templates/rbac.yaml",
        "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: helper\n---\n"
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: Role\nmetadata:\n  name: helper\n"
        "rules: []\n---\n"
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: RoleBinding\nmetadata:\n  name: helper\n"
        "subjects:\n- kind: ServiceAccount\n  name: helper\n- kind: User\n  name: someone@example.com\n"
        "roleRef:\n  apiGroup: rbac.authorization.k8s.io\n  kind: Role\n  name: helper\n")
    _mk(root, "services/helper/helm/templates/secret.yaml",
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: registry\n")


def test_chart_dependencies_and_lock(tmp_path):
    _helm_repo(tmp_path)
    frag = extract_k8s.extract(tmp_path)
    web = _node(frag, "helmchart:services/web/helm")
    assert web["label"] == "web" and web["version"] == "0.2.0" and web["app_version"] == "1.4.0"
    assert web["source_file"] == "services/web/helm/Chart.yaml"
    deps = {e["target"]: e for e in frag["edges"] if e["relation"] == "depends_on"}
    local = deps["helmchart:services/helper/helm"]
    assert local["alias"] == "helperJob" and local["locked_version"] == "0.1.0"
    # the repository URL is an id with userinfo, query and trailing slash stripped
    ext = deps["exthelmchart:https://charts.example.com/stable/cache"]
    assert ext["version"] == "1.x.x" and ext["locked_version"] == "1.2.3"
    assert _node(frag, "exthelmchart:https://charts.example.com/stable/cache")["external"] is True
    assert _node(frag, "helmchart:services/helper/helm")["label"] == "helper"


def test_templates_are_scanned_tolerantly(tmp_path):
    _helm_repo(tmp_path)
    frag = extract_k8s.extract(tmp_path)
    ids, rels = _ids(frag), _rels(frag)
    c = "services/web/helm"
    for kind in ("Deployment", "Secret", "Service", "Ingress"):
        assert f"helmres:{c}/{kind}/web" in ids
        assert (f"helmchart:{c}", "renders", f"helmres:{c}/{kind}/web") in rels
    # a templated name falls back to the file stem and says so
    cm = _node(frag, f"helmres:{c}/ConfigMap/deployment-1")
    assert cm["name_templated"] is True and cm["source_location"].startswith("L")
    dep = _node(frag, f"helmres:{c}/Deployment/web")
    assert dep["values_keys"] == ["image", "resources", "secret"]
    assert dep["source_location"] == "L1"
    assert (f"helmres:{c}/Deployment/web", "reads_value", f"helmvalue:{c}/image") in rels
    assert (f"helmres:{c}/Ingress/web", "reads_value", f"helmvalue:{c}/ingress") in rels
    # a helpers file has no kind: no resource from it
    assert not any(n.get("source_file", "").endswith("_helpers.tpl") for n in frag["nodes"])


def test_template_references_resolve_within_the_chart_and_its_dependencies(tmp_path):
    _helm_repo(tmp_path)
    frag = extract_k8s.extract(tmp_path)
    rels = _rels(frag)
    c, h = "helmres:services/web/helm", "helmres:services/helper/helm"
    assert (f"{c}/Ingress/web", "routes_to", f"{c}/Service/web") in rels
    assert (f"{c}/Deployment/web", "uses_secret", f"{c}/Secret/web") in rels
    # resolved in the local dependency chart, which renders into the same release
    assert (f"{c}/Deployment/web", "uses_secret", f"{h}/Secret/registry") in rels
    # not declared anywhere: a placeholder in the chart's own scope
    assert (f"{c}/Deployment/web", "uses_configmap", f"{c}/ConfigMap/web-config") in rels
    assert _node(frag, f"{c}/ConfigMap/web-config")["declared"] is False
    assert _node(frag, f"{c}/ServiceAccount/web")["declared"] is False
    sel = [e for e in frag["edges"] if e["relation"] == "selects"]
    assert [(e["source"], e["target"], e["confidence"]) for e in sel] == [
        (f"{c}/Service/web", f"{c}/Deployment/web", "INFERRED")]
    via = {e["via"] for e in frag["edges"] if e["source"] == f"{c}/Deployment/web" and "via" in e}
    assert via == {"envFrom", "configMapKeyRef", "imagePullSecrets", "serviceAccountName"}
    assert (f"{h}/RoleBinding/helper", "binds_role", f"{h}/Role/helper") in rels
    assert (f"{h}/RoleBinding/helper", "binds_subject", f"{h}/ServiceAccount/helper") in rels
    # User/Group subjects are identities, not structure: never graphed
    assert "someone" not in json.dumps(frag)


def test_values_files_defaults_overlays_and_keys(tmp_path):
    _helm_repo(tmp_path)
    frag = extract_k8s.extract(tmp_path)
    rels = _rels(frag)
    c = "helmchart:services/web/helm"
    assert ("helmvalues:services/web/helm/values.yaml", "default_values_of", c) in rels
    over = {e["source"]: e for e in frag["edges"] if e["relation"] == "overrides"}
    assert set(over) == {"helmvalues:services/web/values-base.yaml", "helmvalues:services/web/values-prod.yaml"}
    assert all(e["confidence"] == "INFERRED" and e["target"] == c for e in over.values())
    assert ("helmvalues:services/web/values-prod.yaml", "defines_value", "helmvalue:services/web/helm/ingress") in rels
    # a values key named after a subchart's alias configures that subchart
    assert ("helmvalue:services/web/helm/helperJob", "configures", "helmchart:services/helper/helm") in rels


def test_helm_script_release(tmp_path):
    _helm_repo(tmp_path)
    frag = extract_k8s.extract(tmp_path)
    rels = _rels(frag)
    rid = "helmrelease:web/web"
    mod = "module:services/web/deploy/release.sh"
    assert (mod, "deploys", rid) in rels
    assert (rid, "installs", "helmchart:services/web/helm") in rels
    used = sorted((e["order"], e["target"]) for e in frag["edges"] if e["relation"] == "uses_values")
    assert used == [(1, "helmvalues:services/web/values-prod.yaml"),
                    (2, "helmvalues:services/web/values-base.yaml"),
                    (3, "helmvalues:services/web/prod-secrets.yaml")]
    missing = _node(frag, "helmvalues:services/web/prod-secrets.yaml")
    assert missing["missing"] is True and set(missing) == {
        "id", "label", "type", "file_type", "source_file", "missing"}
    rel = _node(frag, rid)
    assert rel["namespace"] == "web" and rel["source_location"] == "L3"
    # the same id and shape the tree-sitter pass gives the script, so both graphs join
    assert _node(frag, mod) == {"id": mod, "label": "services/web/deploy/release.sh", "type": "module",
                                "file_type": "bash", "source_file": "services/web/deploy/release.sh"}


def test_helm_invocation_forms(tmp_path):
    _mk(tmp_path, "deploy/Chart.yaml", "apiVersion: v2\nname: api\nversion: 1.0.0\n")
    _mk(tmp_path, "deploy/values-prod.yaml", "replicas: 2\n")
    _mk(tmp_path, "scripts/release.ps1",
        "# PowerShell, backtick continuations, repo/chart and a chained command\n"
        "helm upgrade --install proxy example/gateway `\n"
        "    --namespace=edge --version 30.1.0 `\n"
        "    --set-string auth.password=" + SECRET + "\n"
        "Set-Location $PSScriptRoot; helm upgrade -i api ..\\deploy -n apps -f ..\\deploy\\values-prod.yaml\n")
    _mk(tmp_path, "scripts/more.sh",
        "set -e\ncd \"$(dirname \"$0\")\" && helm install --generate-name oci://registry.example.com/charts/tool\n"
        "helm install metrics prom --repo https://charts.example.com --timeout 5m\n"
        "helm template ignored ./deploy\n"
        "# helm upgrade --install commented ./deploy\n")
    frag = extract_k8s.extract(tmp_path)
    rels = _rels(frag)
    assert ("module:scripts/release.ps1", "deploys", "helmrelease:edge/proxy") in rels
    assert ("helmrelease:edge/proxy", "installs", "exthelmchart:example/gateway") in rels
    assert ("helmrelease:apps/api", "installs", "helmchart:deploy") in rels
    assert ("helmrelease:apps/api", "uses_values", "helmvalues:deploy/values-prod.yaml") in rels
    assert _node(frag, "module:scripts/release.ps1")["file_type"] == "powershell"
    assert ("helmrelease:-/(generated)", "installs", "exthelmchart:oci://registry.example.com/charts/tool") in rels
    assert ("helmrelease:-/metrics", "installs", "exthelmchart:https://charts.example.com/prom") in rels
    releases = {n["id"] for n in frag["nodes"] if n.get("type") == "helm_release"}
    assert releases == {"helmrelease:edge/proxy", "helmrelease:apps/api",
                        "helmrelease:-/(generated)", "helmrelease:-/metrics"}
    edge = next(e for e in frag["edges"] if e["source"] == "helmrelease:edge/proxy" and e["relation"] == "installs")
    assert edge["version"] == "30.1.0"


def _kustomize_repo(tmp_path):
    _mk(tmp_path, "k8s/base/kustomization.yaml",
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nnamespace: shop\n"
        "resources:\n- app.yaml\n- https://example.com/remote/base?ref=" + SECRET + "\n"
        "secretGenerator:\n- name: db-creds\n  literals:\n  - password=" + SECRET + "\n"
        "helmCharts:\n- name: cache\n  repo: https://charts.example.com\n  version: 1.0.0\n"
        "  releaseName: cache\n  valuesInline:\n    password: " + SECRET + "\n")
    _mk(tmp_path, "k8s/overlay/kustomization.yaml", "resources:\n- ../base\n")
    _mk(tmp_path, "k8s/base/app.yaml",
        "apiVersion: apps/v1\nkind: StatefulSet\nmetadata:\n  name: db\nspec:\n  template:\n"
        "    metadata:\n      labels:\n        app: db\n    spec:\n      containers:\n      - name: db\n"
        "        env:\n        - name: PASSWORD\n          valueFrom:\n            secretKeyRef:\n"
        "              name: db-creds\n              key: password\n"
        "        - name: PLAIN\n          value: " + SECRET + "\n"
        "      volumes:\n      - name: data\n        persistentVolumeClaim:\n          claimName: data\n"
        "      - name: cfg\n        projected:\n          sources:\n          - configMap:\n"
        "              name: db-config\n"
        "---\napiVersion: v1\nkind: Service\nmetadata:\n  name: db\nspec:\n  selector:\n    app: db\n"
        "---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: extra\ndata:\n  key: " + SECRET + "\n"
        "stringData:\n  other: " + SECRET + "\n"
        "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: db-config\ndata:\n  conf: " + SECRET + "\n")
    _mk(tmp_path, "k8s/cluster.yaml",
        "apiVersion: v1\nkind: List\nitems:\n"
        "- apiVersion: rbac.authorization.k8s.io/v1\n  kind: ClusterRole\n  metadata:\n    name: reader\n"
        "- apiVersion: rbac.authorization.k8s.io/v1\n  kind: ClusterRoleBinding\n  metadata:\n"
        "    name: reader\n  roleRef:\n    kind: ClusterRole\n    name: reader\n  subjects:\n"
        "  - kind: ServiceAccount\n    name: bot\n    namespace: ops\n"
        "---\napiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: shop\n  namespace: shop\n"
        "spec:\n  tls:\n  - secretName: shop-tls\n  defaultBackend:\n    service:\n      name: db\n")


def test_plain_manifests_and_kustomize(tmp_path):
    _kustomize_repo(tmp_path)
    frag = extract_k8s.extract(tmp_path)
    rels = _rels(frag)
    k = "kustomization:k8s/base/kustomization.yaml"
    for kind_name in ("StatefulSet/db", "Service/db", "Secret/extra", "ConfigMap/db-config"):
        assert (k, "includes", f"k8sres:shop/{kind_name}") in rels  # namespace from kustomization
    assert ("kustomization:k8s/overlay/kustomization.yaml", "includes", k) in rels
    assert (k, "generates", "k8sres:shop/Secret/db-creds") in rels
    assert ("k8sres:shop/StatefulSet/db", "uses_secret", "k8sres:shop/Secret/db-creds") in rels
    assert ("k8sres:shop/StatefulSet/db", "uses_pvc", "k8sres:shop/PersistentVolumeClaim/data") in rels
    assert ("k8sres:shop/StatefulSet/db", "uses_configmap", "k8sres:shop/ConfigMap/db-config") in rels
    assert ("k8sres:shop/Service/db", "selects", "k8sres:shop/StatefulSet/db") in rels
    assert ("k8sres:shop/Ingress/shop", "routes_to", "k8sres:shop/Service/db") in rels
    assert ("k8sres:shop/Ingress/shop", "uses_secret", "k8sres:shop/Secret/shop-tls") in rels
    assert ("k8sres:-/ClusterRoleBinding/reader", "binds_role", "k8sres:-/ClusterRole/reader") in rels
    assert ("k8sres:-/ClusterRoleBinding/reader", "binds_subject", "k8sres:ops/ServiceAccount/bot") in rels
    assert (k, "deploys", "helmrelease:shop/cache") in rels
    assert ("helmrelease:shop/cache", "installs", "exthelmchart:https://charts.example.com/cache") in rels
    assert _node(frag, "k8sres:shop/Secret/db-creds")["generated"] is True
    assert "declared" not in _node(frag, "k8sres:shop/ConfigMap/db-config")
    # kustomization files are not resources themselves
    assert not any("/Kustomization/" in n["id"] for n in frag["nodes"])


def test_values_never_reach_the_graph(tmp_path):
    _helm_repo(tmp_path)
    _kustomize_repo(tmp_path)
    _mk(tmp_path, "secret.yaml",
        f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: s\ndata:\n  a: {SECRET}\nstringData:\n  b: {SECRET}\n")
    _mk(tmp_path, "cm.yaml",
        f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\ndata:\n  a: {SECRET}\n")
    _mk(tmp_path, "deploy.sh", f"helm upgrade --install r ./services/web/helm --set a={SECRET} --password {SECRET}\n")
    _mk(tmp_path, "kustomization.yaml",
        f"resources:\n- secret.yaml\nconfigMapGenerator:\n- name: g\n  literals:\n  - k={SECRET}\n"
        f"helmCharts:\n- name: x\n  repo: https://u:{SECRET}@charts.example.com\n"
        f"  valuesInline:\n    k: {SECRET}\n")
    frag = extract_k8s.extract(tmp_path)
    assert "helmrelease:-/r" in _ids(frag)
    assert SECRET not in json.dumps(frag)


def test_ansible_files_are_not_manifests(tmp_path):
    # playbooks and task files carrying inline Kubernetes definitions belong to the Ansible pass
    _mk(tmp_path, "site.yml",
        "- hosts: all\n  tasks:\n  - name: apply\n    kubernetes.core.k8s:\n      definition:\n"
        "        apiVersion: v1\n        kind: Namespace\n        metadata:\n          name: shop\n")
    _mk(tmp_path, "roles/app/tasks/main.yml",
        "- name: apply\n  kubernetes.core.k8s:\n    definition:\n      apiVersion: v1\n"
        "      kind: ConfigMap\n      metadata:\n        name: app\n")
    # a stream whose first document is a play list is a playbook, whatever follows it
    _mk(tmp_path, "deploy.yml",
        "- hosts: all\n  roles: [app]\n---\napiVersion: v1\nkind: Namespace\nmetadata:\n  name: shop\n")
    frag = extract_k8s.extract(tmp_path)
    assert frag == {"nodes": [], "edges": []}


def test_robustness_and_prune(tmp_path):
    _mk(tmp_path, "bad.yaml", "apiVersion: v1\nkind: Service\nmetadata: [unclosed\n")
    _mk(tmp_path, "chart/Chart.yaml", ": : not yaml : [\n")
    _mk(tmp_path, "chart/templates/x.yaml", "{{ broken\nkind: Service\nmetadata:\n  name: ok\n")
    # unclosed actions must cost one linear pass, not a quadratic regex scan
    _mk(tmp_path, "chart/templates/braces.yaml", "kind: ConfigMap\n" + "{{ x\n" * 200_000)
    _mk(tmp_path, "node_modules/pkg/svc.yaml", "apiVersion: v1\nkind: Service\nmetadata:\n  name: vendored\n")
    big = tmp_path / "big.yaml"
    big.write_text("apiVersion: v1\nkind: Service\nmetadata:\n  name: big\n" + "#" * (3 * 1024 * 1024),
                   encoding="utf-8")
    (tmp_path / "binary.yaml").write_bytes(b"apiVersion: v1\nkind: Service\n\xff\xfe\x00\x01metadata:\n")
    frag = extract_k8s.extract(tmp_path)
    ids = _ids(frag)
    assert "helmchart:chart" in ids and "helmres:chart/Service/ok" in ids
    assert not any("big" in i or "vendored" in i for i in ids)
    kept = extract_k8s.extract(tmp_path, include=frozenset({"node_modules"}))
    assert "k8sres:-/Service/vendored" in _ids(kept)
    pruned = extract_k8s.extract(tmp_path, exclude=frozenset({"chart"}))
    assert not any(i.startswith("helm") for i in _ids(pruned))


def test_symlink_out_of_root_is_not_read(tmp_path):
    outside = tmp_path / "outside"
    _mk(outside, "svc.yaml", "apiVersion: v1\nkind: Service\nmetadata:\n  name: leaked\n")
    root = tmp_path / "repo"
    root.mkdir()
    try:
        os.symlink(outside / "svc.yaml", root / "svc.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    _mk(root, "deploy.sh", "helm upgrade --install r ./c -f svc.yaml\n")
    frag = extract_k8s.extract(root)
    assert "leaked" not in json.dumps(frag)


def test_output_is_deterministic(tmp_path):
    _helm_repo(tmp_path)
    assert json.dumps(extract_k8s.extract(tmp_path)) == json.dumps(extract_k8s.extract(tmp_path))


def test_orphan_values_file_is_a_node(tmp_path):
    _mk(tmp_path, "values.yaml", f"controller:\n  service:\n    loadBalancerIP: {SECRET}\n")
    frag = extract_k8s.extract(tmp_path)
    assert _ids(frag) == {"helmvalues:values.yaml"} and frag["edges"] == []
    assert SECRET not in json.dumps(frag)


def test_unparsable_chart_url_makes_no_external_node(tmp_path):
    # _repo() gives "" for a URL it cannot parse; that must not become `exthelmchart:`, where
    # every broken reference in the repository would collapse onto one meaningless node
    _mk(tmp_path, "deploy.sh", "helm upgrade --install a http://[bad/chart\n"
                               "helm upgrade --install b chart --repo http://[bad\n")
    frag = extract_k8s.extract(tmp_path)
    assert {"helmrelease:-/a", "helmrelease:-/b"} <= _ids(frag)
    assert not any(i.startswith("exthelmchart:") for i in _ids(frag))
    assert not any(e["relation"] == "installs" for e in frag["edges"])
