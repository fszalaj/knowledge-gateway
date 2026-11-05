"""Kubernetes and Helm extractor - which script deploys which chart, with which values, into
which resources, and what those resources point at.

A generic YAML pass sees a chart as a heap of keys, and cannot read a chart template at all:
Helm templates are Go templates, not YAML. This repo-level pass reads the shapes deployments
are actually written in:

  <chart>/Chart.yaml (+ requirements.yaml)   identity, version, dependencies
  <chart>/Chart.lock (+ requirements.lock)   the version each dependency resolved to
  <chart>/templates/**.yaml|yml|tpl, crds/** each `---` document, scanned tolerantly: its kind,
                                             a literal metadata.name (the file stem when the
                                             name is templated) and the top-level .Values keys
                                             it reads
  <chart>/values.yaml                        the chart's defaults
  <env>-values.yaml, values-<env>.yaml, ...  environment overlays in the chart directory, its
                                             parent or a child of it (linked by proximity)
  *.sh, *.bash, *.zsh, *.ps1                 `helm upgrade [--install]` / `helm install`
                                             invocations, line continuations joined
  any other *.yaml|*.yml                     plain manifests (apiVersion + kind, multi-document,
                                             kind: List items)
  kustomization.yaml|yml, Kustomization      resources/bases/components, namespace, generators,
                                             helmCharts

Emitted nodes:
  helmchart:<chart dir>                 a chart in this repository (or one a reference expects
                                        there, flagged missing=true)
  exthelmchart:<repository>/<name>      a chart from outside: a repo URL (credentials, query and
                                        fragment stripped), an OCI reference or a `repo/chart` alias
  helmres:<chart dir>/<Kind>/<name>     a resource a chart template renders
  helmvalue:<chart dir>/<key>           a top-level values key of a chart (the name only)
  helmvalues:<file>                     a values file; referenced-but-absent files (a git-ignored
                                        secrets file) are flagged missing=true and carry no content
  helmrelease:<namespace or ->/<name>   a release some script or kustomization installs
  k8sres:<namespace or ->/<Kind>/<name> a plain manifest resource (cluster-scoped kinds use `-`)
  kustomization:<file>                  a kustomization
  module:<script>                       the deploying script - the exact id the tree-sitter pass
                                        gives the same file, so the two graphs join

Emitted edges: depends_on (chart -> dependency), default_values_of / overrides (values file ->
chart; overrides is INFERRED from file name and location), defines_value (values file -> key),
reads_value (template resource -> key), configures (key -> the subchart it is named after),
renders (chart -> template resource), deploys (script or kustomization -> release), installs
(release -> chart), uses_values (release -> values file, with `order`), includes / generates
(kustomization -> resources), selects (Service -> workload, INFERRED by labels), routes_to
(Ingress or HTTPRoute -> Service), uses_secret, uses_configmap, uses_service_account, uses_pvc
(each with `via`), binds_role and binds_subject (RoleBinding / ClusterRoleBinding).

Deliberately absent - this pass copies NAMES, never VALUES: no Secret data/stringData, no
ConfigMap data, no env values or env var names, no `--set` arguments, no values-file values
(only top-level key names, and only where a chart gives them structure), no kustomization
literals or valuesInline, no labels, no credentials from repository URLs. Also absent:
template evaluation (Helm or Jinja), remote kustomize bases, patches, RoleBinding User/Group
subjects, namespace inheritance through nested kustomizations, anything requiring a network or
a subprocess. Lists of YAML documents (Ansible playbooks and task files) are never manifests, so
nothing the Ansible pass graphs is graphed twice.
"""
from __future__ import annotations

import os
import posixpath
import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .extract_ansible import skip_dir

_MAX_BYTES = 2 * 1024 * 1024    # larger files are generated or vendored: skip, do not parse
_MAX_DOCS = 500                 # YAML documents read from one file
_MAX_NODES = 2000               # resources emitted from one file
_MAX_KEYS = 200                 # values keys recorded per file / per template document

_SCRIPT_LANG = {".sh": "bash", ".bash": "bash", ".zsh": "bash", ".ps1": "powershell"}
_KUSTOMIZATION = ("kustomization.yaml", "kustomization.yml", "Kustomization")
_CHART_FILES = ("Chart.yaml", "Chart.lock", "requirements.yaml", "requirements.lock")
_VALUES_NAME = re.compile(r"^(?:values|.+[-_.]values|values[-_.].+)\.ya?ml$", re.I)

# Kinds whose objects live outside any namespace; their ids always read `-`.
_CLUSTER_KINDS = frozenset({
    "APIService", "CSIDriver", "CSINode", "ClusterIssuer", "ClusterRole", "ClusterRoleBinding",
    "CustomResourceDefinition", "IngressClass", "MutatingWebhookConfiguration", "Namespace",
    "Node", "PersistentVolume", "PriorityClass", "RuntimeClass", "StorageClass",
    "ValidatingWebhookConfiguration", "VolumeAttachment", "GatewayClass",
})
_WORKLOADS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job",
                        "ReplicationController"})

# Template scanning. A Go template action is replaced by a sentinel, and a line holding only
# actions (`{{- if }}`, `{{ toYaml .Values.x | nindent 2 }}`) is dropped, which leaves YAML in
# every chart written the usual way. Whatever still fails to parse falls back to regexes.
_SENTINEL = "__kg_tpl__"
_ACTION_LINE = re.compile(rf"^[ \t]*(?:{_SENTINEL}[ \t]*)+$", re.M)
_DOC_SPLIT = re.compile(r"^---(?:[ \t].*)?$", re.M)
_VALUES_REF = re.compile(r"\.Values\.([A-Za-z_]\w*)")
_VALUES_INDEX = re.compile(r"index\s+\$?\.Values\s+\"([^\"]+)\"")
_KIND_RE = re.compile(r"^kind:[ \t]*[\"']?([A-Za-z][\w.-]*)", re.M)
_API_RE = re.compile(r"^apiVersion:[ \t]*[\"']?([\w./-]+)", re.M)
_META_RE = re.compile(r"^metadata:[ \t]*\n((?:[ \t]+.*(?:\n|$)|[ \t]*\n)*)", re.M)
_HAS_KIND = re.compile(r"^kind:", re.M)
_HAS_API = re.compile(r"^apiVersion:", re.M)

# helm flags that consume the next argument. A flag missing here would make its argument look
# like the release or chart name, so this is the full value-taking set of install/upgrade.
_HELM_VALUE_FLAGS = frozenset({
    "-n", "--namespace", "-f", "--values", "--version", "--repo", "--set", "--set-file",
    "--set-json", "--set-literal", "--set-string", "--kube-context", "--kubeconfig",
    "--kube-apiserver", "--kube-as-group", "--kube-as-user", "--kube-ca-file", "--kube-token",
    "--timeout", "--description", "--post-renderer", "--post-renderer-args", "-o", "--output",
    "--ca-file", "--cert-file", "--key-file", "--username", "--password", "--registry-config",
    "--repository-cache", "--repository-config", "--history-max", "--name-template",
    "--burst-limit", "--qps", "-l", "--labels",
})
_HELM_BIN = frozenset({"helm", "helm.exe", "helm3"})
_SEPARATORS = frozenset({"&&", "||", ";", "|", "&"})
_SCRIPT_DIR = re.compile(
    r"^\$(?:PSScriptRoot|\{PSScriptRoot\}|\(dirname \$\{?0\}?\)|\(dirname \$\{?BASH_SOURCE(?:\[0\])?\}?\)"
    r"|\{?SCRIPT_DIR\}?)[/\\]")


# --------------------------------------------------------------------------- reading

def _contained(p: Path, root_res: Path) -> Path | None:
    """The resolved path if it stays inside the root - a symlink out of the tree is refused."""
    try:
        rp = p.resolve()
    except (OSError, RuntimeError):
        return None
    return rp if rp.is_relative_to(root_res) else None


def _read(p: Path, root_res: Path) -> str | None:
    rp = _contained(p, root_res)
    try:
        if rp is None or not rp.is_file() or rp.stat().st_size > _MAX_BYTES:
            return None
        return rp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _load(text: str | None):
    if text is None:
        return None
    try:
        return yaml.safe_load(text)
    except Exception:
        return None


def _load_all(text: str) -> list:
    """Every document of a stream; a stream that fails as a whole is retried per document so
    one broken document does not hide its neighbours."""
    try:
        docs = []
        for d in yaml.safe_load_all(text):
            docs.append(d)
            if len(docs) >= _MAX_DOCS:
                break
        return docs
    except Exception:
        pass
    out = []
    for chunk in _DOC_SPLIT.split(text)[:_MAX_DOCS]:
        out.append(_load(chunk))
    return out


def _g(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _lst(x) -> list:
    return x if isinstance(x, list) else []


def _lit(x) -> str | None:
    """A literal Kubernetes name, or None for anything templated, empty or implausible."""
    if not isinstance(x, str):
        return None
    x = x.strip()
    if not x or _SENTINEL in x or "{{" in x or "$" in x or len(x) > 253 or any(c.isspace() for c in x):
        return None
    return x


def _repo(url: str) -> str:
    """A repository reference fit to be an id: userinfo, query and fragment never survive."""
    url = url.strip().rstrip("/")
    if url.startswith("@"):
        return url[1:]
    if url.startswith("alias:"):
        return url[6:]
    if "://" in url:
        try:
            s = urlsplit(url)
            host = s.hostname or ""
            if s.port:
                host += f":{s.port}"
        except ValueError:
            return ""
        return f"{s.scheme}://{host}{s.path}".rstrip("/")
    return url.split("?", 1)[0]


def _join(base: str, arg: str) -> str | None:
    """`arg` relative to the repo-relative directory `base`, or None if it leaves the tree."""
    a = arg.replace("\\", "/")
    if not a or a.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", a):
        return None
    p = posixpath.normpath(posixpath.join(base, a))
    if p == ".." or p.startswith("../"):
        return None
    return p


# --------------------------------------------------------------------------- templates

def _strip_actions(raw: str) -> str:
    """Every `{{ ... }}` action replaced by the sentinel - a linear scan, so a file of unclosed
    braces costs one pass, not the quadratic time a lazy regex would take on it."""
    out, pos = [], 0
    while True:
        a = raw.find("{{", pos)
        if a < 0:
            break
        b = raw.find("}}", a + 2)
        if b < 0:
            break
        out += [raw[pos:a], _SENTINEL]
        pos = b + 2
    out.append(raw[pos:])
    return "".join(out)


def _template_docs(text: str):
    """(line, kind, api, name-or-None, values keys, parsed doc-or-None) per `---` document."""
    pos = 0
    line = 1
    pieces = []
    for m in _DOC_SPLIT.finditer(text):
        pieces.append((line, text[pos:m.start()]))
        line += text[pos:m.end()].count("\n")
        pos = m.end()
    pieces.append((line, text[pos:]))
    for start, raw in pieces[:_MAX_DOCS]:
        keys = sorted({*_VALUES_REF.findall(raw), *_VALUES_INDEX.findall(raw)})[:_MAX_KEYS]
        clean = _ACTION_LINE.sub("", _strip_actions(raw))
        doc = _load(clean)
        if isinstance(doc, dict) and isinstance(doc.get("kind"), str):
            kind, api, name = doc.get("kind"), doc.get("apiVersion"), _g(doc, "metadata", "name")
        else:
            doc = None
            km = _KIND_RE.search(clean)
            if not km:
                continue
            kind = km.group(1)
            am = _API_RE.search(clean)
            api = am.group(1) if am else None
            name = _regex_name(clean)
        if _lit(kind) is None:
            continue
        yield start + len(raw) - len(raw.lstrip("\n")), kind, api, _lit(name), keys, doc


def _regex_name(text: str) -> str | None:
    m = _META_RE.search(text)
    if not m:
        return None
    lines = [ln for ln in m.group(1).splitlines() if ln.strip()]
    if not lines:
        return None
    indent = min(len(ln) - len(ln.lstrip()) for ln in lines)
    for ln in lines:
        if len(ln) - len(ln.lstrip()) == indent and ln.lstrip().startswith("name:"):
            return ln.split(":", 1)[1].strip().strip("\"'")
    return None


# --------------------------------------------------------------------------- references

def _pod(kind: str, doc: dict):
    """(pod spec, pod labels) for a workload, (None, {}) for anything else."""
    if kind == "Pod":
        return _g(doc, "spec"), _g(doc, "metadata", "labels")
    if kind == "CronJob":
        t = _g(doc, "spec", "jobTemplate", "spec", "template")
    elif kind in _WORKLOADS:
        t = _g(doc, "spec", "template")
    else:
        return None, None
    return _g(t, "spec"), _g(t, "metadata", "labels")


def _pod_refs(ps) -> list[tuple[str, str, str, str]]:
    """(kind, name, relation, via). Names only: env entries are read for their *Ref names, never
    their value or their variable name."""
    out: list[tuple[str, str, str, str]] = []
    if not isinstance(ps, dict):
        return out

    def add(kind, name, rel, via):
        n = _lit(name)
        if n:
            out.append((kind, n, rel, via))

    add("ServiceAccount", ps.get("serviceAccountName") or ps.get("serviceAccount"),
        "uses_service_account", "serviceAccountName")
    for s in _lst(ps.get("imagePullSecrets")):
        add("Secret", _g(s, "name"), "uses_secret", "imagePullSecrets")
    for ck in ("initContainers", "containers", "ephemeralContainers"):
        for c in _lst(ps.get(ck)):
            for ef in _lst(_g(c, "envFrom")):
                add("Secret", _g(ef, "secretRef", "name"), "uses_secret", "envFrom")
                add("ConfigMap", _g(ef, "configMapRef", "name"), "uses_configmap", "envFrom")
            for ev in _lst(_g(c, "env")):
                add("Secret", _g(ev, "valueFrom", "secretKeyRef", "name"), "uses_secret", "secretKeyRef")
                add("ConfigMap", _g(ev, "valueFrom", "configMapKeyRef", "name"), "uses_configmap",
                    "configMapKeyRef")
    for v in _lst(ps.get("volumes")):
        add("Secret", _g(v, "secret", "secretName"), "uses_secret", "volume")
        add("ConfigMap", _g(v, "configMap", "name"), "uses_configmap", "volume")
        add("PersistentVolumeClaim", _g(v, "persistentVolumeClaim", "claimName"), "uses_pvc", "volume")
        for src in _lst(_g(v, "projected", "sources")):
            add("Secret", _g(src, "secret", "name"), "uses_secret", "volume")
            add("ConfigMap", _g(src, "configMap", "name"), "uses_configmap", "volume")
    return out


def _refs(kind: str, doc: dict) -> list[tuple[str, str, str, str, str | None]]:
    """(kind, name, relation, via, explicit namespace) for every literal reference in `doc`."""
    out = [(*r, None) for r in _pod_refs(_pod(kind, doc)[0])]
    spec = _g(doc, "spec")
    if kind == "Ingress":
        backs = [_g(spec, "defaultBackend"), _g(spec, "backend")]
        for rule in _lst(_g(spec, "rules")):
            backs += [_g(p, "backend") for p in _lst(_g(rule, "http", "paths"))]
        for b in backs:
            n = _lit(_g(b, "service", "name") or _g(b, "serviceName"))
            if n:
                out.append(("Service", n, "routes_to", "backend", None))
        for t in _lst(_g(spec, "tls")):
            n = _lit(_g(t, "secretName"))
            if n:
                out.append(("Secret", n, "uses_secret", "tls", None))
    elif kind in ("HTTPRoute", "GRPCRoute", "TCPRoute", "TLSRoute", "UDPRoute"):
        for rule in _lst(_g(spec, "rules")):
            for br in _lst(_g(rule, "backendRefs")):
                n = _lit(_g(br, "name"))
                if n and (_g(br, "kind") or "Service") == "Service":
                    out.append(("Service", n, "routes_to", "backendRefs", _lit(_g(br, "namespace"))))
    elif kind in ("RoleBinding", "ClusterRoleBinding"):
        rk, rn = _g(doc, "roleRef", "kind"), _lit(_g(doc, "roleRef", "name"))
        if rk in ("Role", "ClusterRole") and rn:
            out.append((rk, rn, "binds_role", "roleRef", None))
        for s in _lst(_g(doc, "subjects")):
            n = _lit(_g(s, "name"))
            if n and _g(s, "kind") == "ServiceAccount":
                out.append(("ServiceAccount", n, "binds_subject", "subjects", _lit(_g(s, "namespace"))))
    return out


def _selector(kind: str, doc: dict) -> dict | None:
    if kind != "Service":
        return None
    sel = _g(doc, "spec", "selector")
    if not isinstance(sel, dict) or not sel:
        return None
    out = {}
    for k, v in sel.items():
        if not isinstance(k, str) or not isinstance(v, (str, int, bool)) or _SENTINEL in str(v):
            return None
        out[k] = str(v)
    return out


def _labels(kind: str, doc: dict) -> dict:
    labels = _pod(kind, doc)[1]
    if not isinstance(labels, dict):
        return {}
    return {k: str(v) for k, v in labels.items()
            if isinstance(k, str) and isinstance(v, (str, int, bool)) and _SENTINEL not in str(v)}


# --------------------------------------------------------------------------- scripts

def _logical_lines(text: str, ps: bool):
    cont = "`" if ps else "\\"
    buf: list[str] = []
    start = 0
    for i, line in enumerate(text.splitlines(), 1):
        s = line.rstrip()
        if not buf:
            start = i
        if s.endswith(cont) and not (not ps and s.endswith("\\\\")):
            buf.append(s[:-1])
            continue
        buf.append(s)
        yield start, " ".join(buf)
        buf = []
    if buf:
        yield start, " ".join(buf)


def _tokens(line: str, ps: bool) -> list[str]:
    lex = shlex.shlex(line, posix=True)
    lex.whitespace_split = True
    lex.commenters = "#"
    if ps:
        lex.escape = ""
    try:
        return list(lex)
    except ValueError:
        return line.split()


def _commands(tokens: list[str]):
    cur: list[str] = []
    for t in tokens:
        if t in _SEPARATORS:
            if cur:
                yield cur
            cur = []
            continue
        tail = t.endswith(";") and len(t) > 1
        cur.append(t.rstrip(";") if tail else t)
        if tail:
            yield cur
            cur = []
    if cur:
        yield cur


def _helm_calls(text: str, ps: bool):
    """(line, release, namespace, chart arg, values args, version, repo) per install/upgrade."""
    if "helm" not in text:
        return
    for line, logical in _logical_lines(text, ps):
        if "helm" not in logical:
            continue
        for cmd in _commands(_tokens(logical, ps)):
            idx = next((i for i, t in enumerate(cmd)
                        if t.replace("\\", "/").rsplit("/", 1)[-1].lower() in _HELM_BIN), None)
            if idx is None:
                continue
            call = _parse_helm(cmd[idx + 1:])
            if call:
                yield (line, *call)


def _parse_helm(args: list[str]):
    sub = None
    pos: list[str] = []
    ns = version = repo = None
    values: list[str] = []
    generate = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos += args[i + 1:]
            break
        if a.startswith("-") and len(a) > 1:
            name, eq, val = a.partition("=")
            if name in _HELM_VALUE_FLAGS:
                if not eq:
                    i += 1
                    val = args[i] if i < len(args) else ""
                # Only these four are kept; every other value (--set, --password...) is dropped.
                if name in ("-n", "--namespace"):
                    ns = val
                elif name in ("-f", "--values"):
                    values += [v for v in val.split(",") if v]
                elif name == "--version":
                    version = val
                elif name == "--repo":
                    repo = val
            elif name in ("-g", "--generate-name"):
                generate = True
            i += 1
            continue
        if sub is None:
            sub = a
        else:
            pos.append(a)
        i += 1
    if sub == "upgrade" and len(pos) >= 2:
        return pos[0], ns, pos[1], values, version, repo
    if sub == "install":
        if generate and pos:
            return "(generated)", ns, pos[0], values, version, repo
        if len(pos) >= 2:
            return pos[0], ns, pos[1], values, version, repo
    return None


# --------------------------------------------------------------------------- extract

def extract(root: Path, exclude: frozenset[str] = frozenset(),
            include: frozenset[str] = frozenset()) -> dict:
    root = Path(root)
    root_res = root.resolve()
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()

    def add(nid, **attrs):
        cur = nodes.setdefault(nid, {"id": nid})
        for k, v in attrs.items():
            if v is not None and k not in cur:
                cur[k] = v
        return nid

    def edge(src, dst, rel, confidence="EXTRACTED", **extra):
        key = (src, dst, rel)
        if key in seen_edges or src == dst:
            return
        seen_edges.add(key)
        e = {"source": src, "target": dst, "relation": rel, "confidence": confidence}
        e.update({k: v for k, v in extra.items() if v is not None})
        edges.append(e)

    # One pruned, sorted walk feeds every pass below.
    yaml_files: list[str] = []
    scripts: list[str] = []
    chart_dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not skip_dir(d, exclude, include))
        reld = Path(dirpath).relative_to(root).as_posix()
        for fn in sorted(filenames):
            rel = fn if reld == "." else f"{reld}/{fn}"
            low = fn.lower()
            if fn == "Chart.yaml":
                chart_dirs.add(reld)
            if low.endswith((".yaml", ".yml", ".tpl")) or fn == "Kustomization":
                yaml_files.append(rel)
            elif Path(fn).suffix.lower() in _SCRIPT_LANG:
                scripts.append(rel)

    def owner(rel: str) -> str | None:
        d = posixpath.dirname(rel) or "."
        while True:
            if d in chart_dirs:
                return d
            if d == ".":
                return None
            d = posixpath.dirname(d) or "."

    def in_chart(rel: str, chart: str) -> str:
        return rel if chart == "." else rel[len(chart) + 1:]

    def exists_file(rel: str) -> bool:
        rp = _contained(root / rel, root_res)
        return bool(rp and rp.is_file())

    def exists_chart(rel: str) -> bool:
        rp = _contained(root / rel, root_res)
        return bool(rp and (rp / "Chart.yaml").is_file())

    def chart_node(rel: str, missing: bool = False):
        return add(f"helmchart:{rel}", label=posixpath.basename(rel) if rel != "." else (root.name or "chart"),
                   type="helm_chart", file_type="helm",
                   source_file=rel if missing else (f"{rel}/Chart.yaml" if rel != "." else "Chart.yaml"),
                   missing=True if missing else None)

    def ext_chart(ref: str):
        return add(f"exthelmchart:{ref}", label=ref.rsplit("/", 1)[-1], type="helm_chart",
                   file_type="helm", external=True)

    # ---- charts -------------------------------------------------------------------------
    chart_deps: dict[str, list[str]] = {}          # chart -> local dependency charts
    chart_subkeys: dict[str, dict[str, str]] = {}  # chart -> values key -> dependency node
    metas: dict[str, dict] = {}

    def ver(x):
        return str(x) if isinstance(x, (str, int, float)) and not isinstance(x, bool) else None

    # Identity first: a dependency must find its target's real node, not invent one.
    for c in sorted(chart_dirs):
        base = "" if c == "." else f"{c}/"
        meta = _load(_read(root / f"{base}Chart.yaml", root_res))
        meta = metas[c] = meta if isinstance(meta, dict) else {}
        cname = meta.get("name") if isinstance(meta.get("name"), str) else None
        add(f"helmchart:{c}", label=cname or (posixpath.basename(c) if c != "." else root.name),
            type="helm_chart", file_type="helm", source_file=f"{base}Chart.yaml",
            version=ver(meta.get("version")), app_version=ver(meta.get("appVersion")),
            chart_type=meta.get("type") if isinstance(meta.get("type"), str) else None)
    for c in sorted(chart_dirs):
        base = "" if c == "." else f"{c}/"
        meta = metas[c]
        cid = f"helmchart:{c}"
        deps = _lst(meta.get("dependencies"))
        if not deps:
            req = _load(_read(root / f"{base}requirements.yaml", root_res))
            deps = _lst(_g(req, "dependencies"))
        lock = _load(_read(root / f"{base}Chart.lock", root_res)) or _load(
            _read(root / f"{base}requirements.lock", root_res))
        locked = {d.get("name"): str(d.get("version")) for d in _lst(_g(lock, "dependencies"))
                  if isinstance(d, dict) and isinstance(d.get("name"), str) and d.get("version") is not None}
        chart_deps[c] = []
        chart_subkeys[c] = {}
        for d in deps:
            if not isinstance(d, dict) or not _lit(d.get("name")):
                continue
            dname = d["name"]
            repo = d.get("repository") if isinstance(d.get("repository"), str) else ""
            if repo.startswith("file://"):
                p = _join(c, repo[7:])
                if p is None:
                    continue
                tid = chart_node(p, missing=not exists_chart(p))
                chart_deps[c].append(p)
            elif not repo.strip():
                p = posixpath.normpath(f"{base}charts/{dname}")
                if exists_chart(p):
                    tid = chart_node(p)
                    chart_deps[c].append(p)
                else:
                    tid = ext_chart(dname)
            else:
                r = _repo(repo)
                if not r:
                    continue
                tid = ext_chart(f"{r}/{dname}")
                nodes[tid].setdefault("repository", r)
            alias = d.get("alias") if isinstance(d.get("alias"), str) else None
            edge(cid, tid, "depends_on", version=ver(d.get("version")),
                 locked_version=locked.get(dname), alias=alias)
            chart_subkeys[c][alias or dname] = tid

    def scope_charts(c: str) -> list[str]:
        """The chart and every local chart it depends on - they render into one release."""
        out, todo = [], [c]
        while todo:
            x = todo.pop(0)
            if x not in out:
                out.append(x)
                todo += chart_deps.get(x, [])
        return out

    # ---- values files -------------------------------------------------------------------
    values_keys: dict[str, list[str]] = {}

    def values_node(rel: str) -> str:
        vid = f"helmvalues:{rel}"
        if vid in nodes:
            return vid
        if "$" in rel or "{" in rel:
            return add(vid, label=posixpath.basename(rel), type="helm_values", file_type="helm",
                       source_file=rel, unresolved=True)
        if not exists_file(rel):
            return add(vid, label=posixpath.basename(rel), type="helm_values", file_type="helm",
                       source_file=rel, missing=True)
        data = _load(_read(root / rel, root_res))
        values_keys[rel] = sorted(k for k in data if isinstance(k, str))[:_MAX_KEYS] if isinstance(data, dict) else []
        return add(vid, label=posixpath.basename(rel), type="helm_values", file_type="helm", source_file=rel)

    def value_key(chart: str, key: str) -> str:
        return add(f"helmvalue:{chart}/{key}", label=key, type="helm_value", file_type="helm",
                   source_file=f"{chart}/Chart.yaml" if chart != "." else "Chart.yaml")

    def bind_values(rel: str, chart: str):
        for k in values_keys.get(rel, []):
            edge(f"helmvalues:{rel}", value_key(chart, k), "defines_value")

    for rel in yaml_files:
        fn = posixpath.basename(rel)
        if not _VALUES_NAME.match(fn):
            continue
        c = owner(rel)
        if c is not None and in_chart(rel, c).split("/", 1)[0] in ("templates", "crds", "charts"):
            continue
        d = posixpath.dirname(rel) or "."
        if c == d and fn in ("values.yaml", "values.yml"):
            text = _read(root / rel, root_res)
            if text is not None and _HAS_KIND.search(text) and _HAS_API.search(text):
                continue
            vid = values_node(rel)
            edge(vid, f"helmchart:{c}", "default_values_of")
            bind_values(rel, c)
            continue
        cands = sorted(x for x in chart_dirs
                       if x == d or (posixpath.dirname(x) or ".") == d or (posixpath.dirname(d) or ".") == x)
        if len(cands) > 1:
            stem = fn.rsplit(".", 1)[0].lower()
            cands = [x for x in cands if posixpath.basename(x).lower() in stem
                     or str(nodes[f"helmchart:{x}"].get("label", "")).lower() in stem]
        text = _read(root / rel, root_res)
        if text is None:
            continue
        data = _load(text)
        if not isinstance(data, dict) or ("apiVersion" in data and "kind" in data):
            continue
        vid = values_node(rel)
        if len(cands) == 1:
            edge(vid, f"helmchart:{cands[0]}", "overrides", "INFERRED")
            bind_values(rel, cands[0])

    # ---- helm invocations in scripts ----------------------------------------------------
    def resolve_local(sdir: str, arg: str, want) -> str | None:
        a = _SCRIPT_DIR.sub("", arg.replace("\\", "/"))
        first = _join(sdir, a)
        if first is not None and want(first):
            return first
        second = _join(".", a)
        if second is not None and want(second):
            return second
        return first

    def release(src: str, rname: str, ns, chart_arg, vals, version, repo, sdir, srel, line):
        rid = add(f"helmrelease:{ns or '-'}/{rname}", label=rname, type="helm_release",
                  file_type="helm", namespace=ns or None, source_file=srel,
                  source_location=f"L{line}" if line else None)
        edge(src, rid, "deploys")
        local = None
        # an unparsable URL yields "" from _repo(): no node, as for a Chart.yaml dependency -
        # otherwise every broken reference would collapse onto one meaningless `exthelmchart:`
        if "://" in chart_arg:
            r = _repo(chart_arg)
            tid = ext_chart(r) if r else None
        elif repo:
            r = _repo(repo)
            tid = ext_chart(f"{r}/{chart_arg}") if r else None
            if tid:
                nodes[tid].setdefault("repository", r)
        else:
            p = resolve_local(sdir, chart_arg, exists_chart)
            if p is not None and exists_chart(p):
                tid = chart_node(p)
                local = p
            elif p is not None and p.endswith(".tgz") and exists_file(p):
                tid = add(f"helmchart:{p}", label=posixpath.basename(p), type="helm_chart",
                          file_type="helm", source_file=p, packaged=True)
            elif chart_arg.count("/") == 1 and not chart_arg.startswith((".", "$")):
                tid = ext_chart(chart_arg)
            elif p is not None:
                tid = chart_node(p, missing=True)
            else:
                tid = None
        if tid:
            edge(rid, tid, "installs", version=version or None)
        for order, v in enumerate(vals, 1):
            if v == "-" or "://" in v:
                continue
            p = resolve_local(sdir, v, exists_file)
            if p is None:
                continue
            vid = values_node(p)
            edge(rid, vid, "uses_values", order=order)
            if local:
                bind_values(p, local)
        return rid

    for srel in scripts:
        lang = _SCRIPT_LANG[Path(srel).suffix.lower()]
        text = _read(root / srel, root_res)
        if not text:
            continue
        sdir = posixpath.dirname(srel) or "."
        mod = f"module:{srel}"
        count = 0
        for line, rname, ns, chart_arg, vals, version, repo in _helm_calls(text, lang == "powershell"):
            if count >= _MAX_NODES:
                break
            count += 1
            add(mod, label=srel, type="module", file_type=lang, source_file=srel)
            release(mod, rname, ns, chart_arg, vals, version, repo, sdir, srel, line)

    # ---- kustomizations -----------------------------------------------------------------
    ns_of_file: dict[str, str] = {}
    includes: list[tuple[str, str]] = []           # (kustomization id, manifest file)
    kust_files = [r for r in yaml_files if posixpath.basename(r) in _KUSTOMIZATION]
    kust_set = set(kust_files)
    for krel in kust_files:
        k = _load(_read(root / krel, root_res))
        if not isinstance(k, dict):
            continue
        kdir = posixpath.dirname(krel) or "."
        kid = add(f"kustomization:{krel}", label=krel, type="kustomization",
                  file_type="kubernetes", source_file=krel)
        kns = _lit(k.get("namespace"))
        for key in ("resources", "bases", "components"):
            for entry in _lst(k.get(key)):
                if not isinstance(entry, str) or "://" in entry or "?" in entry or entry.startswith("github.com/"):
                    continue  # remote bases are never fetched
                p = _join(kdir, entry)
                if p is None:
                    continue
                sub = next((f"{p}/{n}" if p != "." else n for n in _KUSTOMIZATION
                            if (f"{p}/{n}" if p != "." else n) in kust_set), None)
                if sub:
                    edge(kid, add(f"kustomization:{sub}", label=sub, type="kustomization",
                                  file_type="kubernetes", source_file=sub), "includes")
                elif exists_file(p):
                    includes.append((kid, p))
                    if kns:
                        ns_of_file.setdefault(p, kns)
        for gen, gkind in (("configMapGenerator", "ConfigMap"), ("secretGenerator", "Secret")):
            for g in _lst(k.get(gen)):
                n = _lit(_g(g, "name"))
                if n:  # the name only: literals, files and envs are values
                    gns = _lit(_g(g, "namespace")) or kns or "-"
                    gid = add(f"k8sres:{gns}/{gkind}/{n}", label=f"{gkind}/{n}", type=gkind.lower(),
                              kind=gkind, file_type="kubernetes", source_file=krel, generated=True)
                    edge(kid, gid, "generates")
        home = k.get("helmGlobals", {}).get("chartHome") if isinstance(k.get("helmGlobals"), dict) else None
        for hc in _lst(k.get("helmCharts")):
            name = _lit(_g(hc, "name"))
            if not name:
                continue
            rname = _lit(_g(hc, "releaseName")) or name
            hns = _lit(_g(hc, "namespace")) or kns
            repo = _g(hc, "repo") if isinstance(_g(hc, "repo"), str) else None
            vals = [v for v in [_g(hc, "valuesFile"), *_lst(_g(hc, "additionalValuesFiles"))] if isinstance(v, str)]
            chart_arg = name
            if not repo:  # a local chart under chartHome; "./" keeps it from reading as repo/chart
                chart_arg = f"./{home if isinstance(home, str) else 'charts'}/{name}"
            release(kid, rname, hns, chart_arg, vals, _g(hc, "version") if isinstance(_g(hc, "version"), str) else None,
                    repo, kdir, krel, None)

    # ---- resources: chart templates and plain manifests ---------------------------------
    resources: list[dict] = []        # id, kind, chart, ns, parsed doc - the first declaration wins
    res_ids: set[str] = set()
    by_file: dict[str, list[str]] = {}

    for rel in yaml_files:
        if posixpath.basename(rel) in _KUSTOMIZATION:
            continue
        c = owner(rel)
        top = in_chart(rel, c).split("/", 1)[0] if c is not None else None
        if c is not None and top in ("templates", "crds"):
            text = _read(root / rel, root_res)
            if text is None:
                continue
            stem = posixpath.basename(rel).split(".", 1)[0]
            n_emitted = 0
            for line, kind, api, name, keys, doc in _template_docs(text):
                if n_emitted >= _MAX_NODES:
                    break
                templated = name is None
                if templated:
                    name = stem if n_emitted == 0 else f"{stem}-{n_emitted}"
                rid = f"helmres:{c}/{kind}/{name}"
                add(rid, label=f"{kind}/{name}", type=kind.lower(), kind=kind,
                    api_version=api if isinstance(api, str) else None, file_type="helm",
                    source_file=rel, source_location=f"L{line}",
                    name_templated=True if templated else None, values_keys=keys or None)
                n_emitted += 1
                edge(f"helmchart:{c}", rid, "renders")
                for key in keys:
                    edge(rid, value_key(c, key), "reads_value")
                if rid not in res_ids:
                    res_ids.add(rid)
                    resources.append({"id": rid, "kind": kind, "chart": c, "ns": None,
                                      "doc": doc if isinstance(doc, dict) else None})
            continue
        if posixpath.basename(rel) in _CHART_FILES or rel.endswith(".tpl"):
            continue
        text = _read(root / rel, root_res)
        if text is None or not _HAS_KIND.search(text) or not _HAS_API.search(text):
            continue
        docs = _load_all(text)
        if docs and isinstance(docs[0], list):
            continue  # a playbook or task file: the Ansible pass owns it
        flat = []
        for d in docs:
            if isinstance(d, dict) and isinstance(d.get("kind"), str) and str(d["kind"]).endswith("List") \
                    and isinstance(d.get("items"), list):
                flat += [x for x in d["items"] if isinstance(x, dict)]
            else:
                flat.append(d)
        n_emitted = 0
        for d in flat:
            if n_emitted >= _MAX_NODES:
                break
            if not isinstance(d, dict) or not isinstance(d.get("apiVersion"), str):
                continue
            kind, name = _lit(d.get("kind")), _lit(_g(d, "metadata", "name"))
            if not kind or not name or kind in ("Kustomization", "Component"):
                continue
            ns = "-" if kind in _CLUSTER_KINDS else (
                _lit(_g(d, "metadata", "namespace")) or ns_of_file.get(rel) or "-")
            rid = f"k8sres:{ns}/{kind}/{name}"
            add(rid, label=f"{kind}/{name}", type=kind.lower(), kind=kind, api_version=d["apiVersion"],
                namespace=None if ns == "-" else ns, file_type="kubernetes", source_file=rel)
            n_emitted += 1
            by_file.setdefault(rel, []).append(rid)
            if rid not in res_ids:
                res_ids.add(rid)
                resources.append({"id": rid, "kind": kind, "chart": None, "ns": ns, "doc": d})

    for kid, p in includes:
        for rid in by_file.get(p, []):
            edge(kid, rid, "includes")

    # ---- references between resources ---------------------------------------------------
    def target(res: dict, kind: str, name: str, ns: str | None) -> str:
        if res["chart"] is not None:
            for c in scope_charts(res["chart"]):
                tid = f"helmres:{c}/{kind}/{name}"
                if tid in nodes:
                    return tid
            tid = f"helmres:{res['chart']}/{kind}/{name}"
            return add(tid, label=f"{kind}/{name}", type=kind.lower(), kind=kind,
                       file_type="helm", declared=False)
        tns = "-" if kind in _CLUSTER_KINDS else (ns or res["ns"])
        tid = f"k8sres:{tns}/{kind}/{name}"
        if tid in nodes:  # declared (or generated) - never stamp declared=False on it
            return tid
        return add(tid, label=f"{kind}/{name}", type=kind.lower(), kind=kind,
                   namespace=None if tns == "-" else tns, file_type="kubernetes", declared=False)

    def scope(res: dict):
        return ("chart", tuple(scope_charts(res["chart"]))) if res["chart"] is not None else ("ns", res["ns"])

    workloads: list[tuple[dict, dict]] = []
    for res in resources:
        if res["doc"] is not None:
            lab = _labels(res["kind"], res["doc"])
            if lab:
                workloads.append((res, lab))

    for res in resources:
        doc = res["doc"]
        if doc is None:
            continue
        for kind, name, rel, via, ns in _refs(res["kind"], doc):
            edge(res["id"], target(res, kind, name, ns), rel, via=via)
        sel = _selector(res["kind"], doc)
        if sel:
            sc = scope(res)
            for w, lab in workloads:
                same = (w["chart"] in sc[1]) if sc[0] == "chart" else (w["chart"] is None and w["ns"] == sc[1])
                if same and all(lab.get(k) == v for k, v in sel.items()):
                    edge(res["id"], w["id"], "selects", "INFERRED")

    # ---- values keys named after a subchart configure that subchart ---------------------
    for c, keys in chart_subkeys.items():
        for key, tid in keys.items():
            vid = f"helmvalue:{c}/{key}"
            if vid in nodes:
                edge(vid, tid, "configures")

    return {"nodes": list(nodes.values()), "edges": edges}
