"""Dependency-manifest extractor - what a repository declares it needs, and how its projects nest.

A generic AST pass sees `import fastapi` but not that pyproject.toml pins it, that two .NET
projects reference each other, or that a playbook repository pulls a Galaxy collection. This
repo-level pass reads the declaration files themselves (stdlib + PyYAML, no tree-sitter):

  *.csproj / *.fsproj / *.vbproj (any *proj)  Sdk, TargetFramework(s), OutputType,
                                              PackageReference, ProjectReference, Compile
  *.sln (text) / *.slnx (XML)                 the project entries
  pyproject.toml                              [project] dependencies + optional-dependencies,
                                              PEP 735 [dependency-groups], [build-system]
                                              requires, [tool.poetry] dependency tables
  requirements*.txt (also requirements/*.txt) requirement lines, -r / -c includes
  package.json                                dependencies, devDependencies, peer, optional
  requirements.yml / requirements.yaml        Ansible Galaxy collections and roles

Emitted nodes:
  project:<rel>              an MSBuild project (type dotnet_project); sdk, target_frameworks,
                             output_type
  solution:<rel>             a .sln/.slnx solution
  manifest:<rel>             pyproject.toml / requirements*.txt / package.json / requirements.yml
                             (type pyproject | requirements | package_json | galaxy_requirements);
                             package_name for pyproject.toml and package.json
  extpackage:<eco>:<name>    an external package; eco is nuget, pypi, npm or galaxy. PyPI names
                             are PEP 503-normalised and NuGet ids lower-cased (both registries
                             are case-insensitive), so every manifest that names a package joins
                             on one node
  module:<rel>               a C# source file, the same id the tree-sitter pass gives it

Emitted edges: depends_on (manifest/project -> extpackage; attrs version or specifier, scope
main|dev|optional|group|build|peer, group, extras, kind collection|role, origin
direct_reference|git|url|scm|local|workspace|alias, private for PrivateAssets=all), references_project
(project -> project), contains (solution -> project; project -> module for .cs files, INFERRED
from the SDK's default glob: the nearest enclosing project owns a file, bin/ and obj/ excluded;
EXTRACTED from explicit <Compile Include> when default items are switched off), includes and
constrained_by (requirements file -> the file its -r / -c names).

Galaxy -> role join: galaxy_provides() is this pass's rule for which Galaxy requirement provides
which Ansible role (a role entry the role of the same name; a collection `ns.coll` each
`ns.coll.<role>` reference). Only the Ansible pass knows which roles a playbook or task names, so
it calls the rule and emits the `provides` edges (extpackage:galaxy:<name> -> role:<name>,
INFERRED), typing a role that is referenced but not defined in the repository.

Deliberately absent: anything that could carry a credential or a private location. Index URLs,
--extra-index-url and other pip options, direct-reference URLs (`name @ https://...`), git/url
sources in package.json, Poetry and Galaxy entries are reduced to the package NAME (the source
kind becomes an `origin` attr); version strings are kept only when they are plain specifiers. No
UserSecretsId or other MSBuild property values beyond the few listed above. Nothing is resolved
against a registry: no network, no subprocess, no template evaluation. XML is parsed with
xml.etree under the size cap, and a document declaring a DOCTYPE is skipped outright.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from .extract_ansible import skip_dir

MAX_BYTES = 2 * 1024 * 1024   # a larger manifest is generated output (lock files), not a declaration
MAX_DEPS = 2000               # dependency / reference edges per manifest
MAX_CONTAINS = 5000           # inferred project -> source-file edges per project
_DOTNET_OUTPUT = {"bin", "obj"}
_SLN_FOLDER = "2150E333-8FDC-42A3-9474-1A3956D46DE8"   # a solution folder, not a project
# `[ \t]*`, not `\s*`: under re.M a `\s*` at each line start also eats the newlines that follow
# and backtracks through them, quadratic in the length of a blank run.
_SLN_PROJECT = re.compile(r'^[ \t]*Project\("\{([0-9A-Fa-f-]+)\}"\)\s*=\s*"[^"]*"\s*,\s*"([^"]+)"', re.M)

_PEP508 = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(\[[^\]]*\])?\s*(.*)$", re.S)
_SPECIFIER = re.compile(r"[\w.*+!<>=~,\s]*")         # PEP 440 specifier characters only
_PLAIN_VERSION = re.compile(r"[\w.*+!<>=~^|,\s-]{1,80}")  # npm/poetry/galaxy ranges, no ':' or '/'
_NUGET_VERSION = re.compile(r"[\w.*$()\[\],\s-]{1,80}")   # 1.2.3, [1.0,2.0), $(Prop)
_NPM_NAME = re.compile(r"^(@[A-Za-z0-9~][\w.~-]*/)?[A-Za-z0-9~][\w.~-]*$")
_GALAXY_NAME = re.compile(r"^[A-Za-z0-9_][\w.-]*$")
_ARCHIVES = (".whl", ".tar.gz", ".zip", ".tgz", ".tar.bz2")


# --------------------------------------------------------------------------------------------
# identity: which files this pass owns, and the node id each one gets. The docker pass imports
# node_for() so a `COPY pyproject.toml .` lands on the same node this pass describes.

def manifest_kind(rel: str) -> str | None:
    name = posixpath.basename(rel)
    low = name.lower()
    if low.endswith("proj") and "." in low[:-4]:
        return "msbuild"
    if low.endswith((".sln", ".slnx")):
        return "solution"
    if low == "pyproject.toml":
        return "pyproject"
    if low == "package.json":
        return "package_json"
    if low in ("requirements.yml", "requirements.yaml"):
        return "galaxy_requirements"
    if low.endswith(".txt") and ("requirements" in low[:-4]
                                 or posixpath.basename(posixpath.dirname(rel)).lower() == "requirements"):
        return "requirements"
    return None


def node_for(rel: str) -> dict | None:
    """The node this pass emits for `rel`, or None when `rel` is not a manifest."""
    kind = manifest_kind(rel)
    label = posixpath.basename(rel)
    if kind == "msbuild":
        return {"id": f"project:{rel}", "label": label.rsplit(".", 1)[0], "type": "dotnet_project",
                "file_type": "msbuild", "source_file": rel}
    if kind == "solution":
        return {"id": f"solution:{rel}", "label": label, "type": "solution",
                "file_type": "msbuild", "source_file": rel}
    if kind:
        return {"id": f"manifest:{rel}", "label": label, "type": kind,
                "file_type": "manifest", "source_file": rel}
    return None


# --------------------------------------------------------------------------------------------
# helpers

def _read(root_res: Path, path: Path) -> str | None:
    """File text, or None when it is missing, too large, unreadable or a link out of the repo."""
    try:
        real = path.resolve(strict=True)
        if not real.is_relative_to(root_res) or not real.is_file():
            return None
        if real.stat().st_size > MAX_BYTES:
            return None
        return real.read_text(encoding="utf-8", errors="replace").lstrip("﻿")  # VS writes a BOM
    except (OSError, RuntimeError, ValueError):
        return None


def _join(base: str, p) -> str | None:
    """`p` (relative to repo directory `base`) as a repo-relative POSIX path; None when it is not
    a literal local path or would leave the repository."""
    if not isinstance(p, str):
        return None
    p = p.strip().replace("\\", "/")
    if (not p or "$" in p or "://" in p or "%" in p or p.startswith(("/", "~"))
            or re.match(r"^[A-Za-z]:", p)):
        return None
    out = posixpath.normpath(posixpath.join(base, p))
    if out == ".":
        return ""
    if out == ".." or out.startswith(("../", "/")):
        return None
    return out


def _is_file(root_res: Path, rel: str) -> bool:
    try:
        real = (root_res / rel).resolve(strict=True)
        return real.is_relative_to(root_res) and real.is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def package_node(eco: str, key: str, label: str, rel: str) -> dict:
    """The extpackage:<eco>:<key> node; the Ansible pass emits Galaxy packages in this shape too."""
    return {"id": f"extpackage:{eco}:{key}", "label": label, "type": "package",
            "file_type": eco, "source_file": rel, "ecosystem": eco}


def _pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pep508(req) -> tuple[str, dict] | None:
    """(normalised name, edge attrs) of one PEP 508 requirement; a URL never reaches the attrs."""
    if not isinstance(req, str):
        return None
    m = _PEP508.match(req)
    if not m:
        return None
    name, extras, rest = m.group(1), m.group(2), m.group(3)
    if name.lower().endswith(_ARCHIVES):
        return None
    attrs: dict = {}
    if extras:
        ex = [e.strip() for e in extras[1:-1].split(",") if re.fullmatch(r"[\w.-]+", e.strip())]
        if ex:
            attrs["extras"] = ex
    rest = rest.split(";", 1)[0].strip()
    if rest.startswith("@"):
        attrs["origin"] = "direct_reference"
    else:
        spec = rest.strip("() ")
        if spec and not _SPECIFIER.fullmatch(spec):
            return None            # a path, a URL or something else that is not a requirement
        if spec:
            attrs["specifier"] = spec
    return _pypi(name), attrs


# --------------------------------------------------------------------------------------------
# the pass

def extract(root: Path, exclude: frozenset[str] = frozenset(),
            include: frozenset[str] = frozenset()) -> dict:
    root = Path(root)
    root_res = root.resolve()
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    seen_edges: set[tuple] = set()

    def add(n: dict) -> str:
        cur = nodes.setdefault(n["id"], dict(n))
        for k, v in n.items():
            if v is not None and k not in cur:
                cur[k] = v
        return n["id"]

    def edge(src, dst, rel, confidence="EXTRACTED", **attrs):
        key = (src, dst, rel, tuple(sorted((k, str(v)) for k, v in attrs.items())))
        if key in seen_edges:
            return
        seen_edges.add(key)
        e = {"source": src, "target": dst, "relation": rel, "confidence": confidence}
        e.update({k: v for k, v in attrs.items()
                  if v is not None and k not in ("source", "target", "relation", "confidence")})
        edges.append(e)

    def package(eco: str, key: str, label: str, rel: str) -> str:
        return add(package_node(eco, key, label, rel))

    manifests: list[tuple[Path, str, str]] = []
    cs_files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not skip_dir(d, exclude, include))
        d = Path(dirpath)
        drel = "" if d == root else d.relative_to(root).as_posix()
        in_output = bool(set(drel.split("/")) & _DOTNET_OUTPUT) if drel else False
        for fn in sorted(filenames):
            rel = f"{drel}/{fn}" if drel else fn
            kind = manifest_kind(rel)
            if kind:
                manifests.append((d / fn, rel, kind))
            elif fn.lower().endswith(".cs") and not in_output:
                cs_files.append(rel)

    owners: dict[str, list[str]] = {}       # project dir -> projects that glob its .cs files

    for path, rel, kind in manifests:
        text = _read(root_res, path)
        if text is None:
            continue
        base = posixpath.dirname(rel)
        me = node_for(rel)
        try:
            if kind == "msbuild":
                _msbuild(text, rel, base, me, root_res, add, edge, package, owners)
            elif kind == "solution":
                add(me)
                _solution(text, rel, base, me["id"], root_res, add, edge)
            elif kind == "pyproject":
                _pyproject(text, rel, base, me, root_res, add, edge, package)
            elif kind == "requirements":
                add(me)
                _requirements(text, rel, base, me["id"], root_res, add, edge, package)
            elif kind == "package_json":
                _package_json(text, rel, me, add, edge, package)
            elif kind == "galaxy_requirements":
                _galaxy(text, rel, me, add, edge, package)
        except Exception:  # a malformed manifest is one file's problem, never the build's
            continue

    # Inferred ownership of C# sources: the SDK globs **/*.cs under the project directory, and a
    # nested project takes its own subtree. Projects with default items off claim their subtree
    # too (with no edges) so an outer project does not inherit files it does not compile.
    counts: dict[str, int] = {}
    for rel in cs_files:
        d = posixpath.dirname(rel)
        while True:
            if d in owners:
                for pid in owners[d]:
                    if counts.get(pid, 0) >= MAX_CONTAINS:
                        continue
                    counts[pid] = counts.get(pid, 0) + 1
                    add({"id": f"module:{rel}", "label": rel, "type": "module",
                         "file_type": "csharp", "source_file": rel})
                    edge(pid, f"module:{rel}", "contains", "INFERRED")
                break
            if not d:
                break
            d = posixpath.dirname(d)

    return {"nodes": list(nodes.values()), "edges": edges}


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _xml(text: str):
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        return None          # MSBuild never needs a DTD; refuse entity games outright
    return ET.fromstring(text)


def _msbuild(text, rel, base, me, root_res, add, edge, package, owners):
    doc = _xml(text)
    if doc is None:
        return
    frameworks: list[str] = []
    output_type = None
    default_items = True
    explicit: list[str] = []
    n = 0
    pid = me["id"]
    for el in doc.iter():
        t = _local(el.tag)
        val = (el.text or "").strip()
        if t in ("TargetFramework", "TargetFrameworks") and val:
            frameworks += [f.strip() for f in val.split(";")
                           if re.fullmatch(r"[\w.$()-]{1,40}", f.strip())]
        elif t == "OutputType" and re.fullmatch(r"\w{1,20}", val):
            output_type = val
        elif t == "EnableDefaultCompileItems" and val.lower() == "false":
            default_items = False
        elif t == "Compile" and el.get("Include"):
            explicit += [p for p in el.get("Include").split(";") if p.strip()]
        elif t == "PackageReference" and n < MAX_DEPS:
            name = (el.get("Include") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_][\w.-]{0,200}", name):
                continue
            ver = el.get("Version") or el.get("VersionOverride")
            if ver is None:
                for c in el:
                    if _local(c.tag) in ("Version", "VersionOverride") and c.text:
                        ver = c.text
                        break
            ver = ver.strip() if isinstance(ver, str) else None
            private = (el.get("PrivateAssets") or "").strip().lower() == "all" or any(
                _local(c.tag) == "PrivateAssets" and (c.text or "").strip().lower() == "all"
                for c in el)
            tgt = package("nuget", name.lower(), name, rel)
            edge(pid, tgt, "depends_on",
                 version=ver if ver and _NUGET_VERSION.fullmatch(ver) else None,
                 private=True if private else None)
            n += 1
        elif t == "ProjectReference" and n < MAX_DEPS:
            target = _join(base, el.get("Include"))
            if target and _is_file(root_res, target):
                other = node_for(target) or {"id": f"project:{target}", "label": target,
                                             "type": "dotnet_project", "file_type": "msbuild",
                                             "source_file": target}
                edge(pid, add(other), "references_project")
                n += 1
    sdk = doc.get("Sdk")
    node = dict(me)
    if sdk and re.fullmatch(r"[\w./-]{1,100}", sdk):
        node["sdk"] = sdk
    if frameworks:
        node["target_frameworks"] = list(dict.fromkeys(frameworks))
    if output_type:
        node["output_type"] = output_type
    add(node)
    if rel.lower().endswith(".csproj"):
        owners.setdefault(base, [])
        if default_items:
            owners[base].append(pid)
        else:
            for p in explicit[:MAX_CONTAINS]:
                if any(c in p for c in "*?"):
                    continue
                src = _join(base, p)
                if src and src.lower().endswith(".cs") and _is_file(root_res, src):
                    add({"id": f"module:{src}", "label": src, "type": "module",
                         "file_type": "csharp", "source_file": src})
                    edge(pid, f"module:{src}", "contains")


def _solution(text, rel, base, sid, root_res, add, edge):
    paths: list[str] = []
    if rel.lower().endswith(".slnx"):
        doc = _xml(text)
        if doc is None:
            return
        paths = [el.get("Path") for el in doc.iter() if _local(el.tag) == "Project" and el.get("Path")]
    else:
        paths = [m.group(2) for m in _SLN_PROJECT.finditer(text) if m.group(1).upper() != _SLN_FOLDER]
    for p in paths[:MAX_DEPS]:
        target = _join(base, p)
        if target and manifest_kind(target) == "msbuild" and _is_file(root_res, target):
            edge(sid, add(node_for(target)), "contains")


def _pyproject(text, rel, base, me, root_res, add, edge, package):
    data = tomllib.loads(text)
    node = dict(me)
    proj = data.get("project") if isinstance(data.get("project"), dict) else {}
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}
    pname = proj.get("name") or poetry.get("name")
    if isinstance(pname, str) and re.fullmatch(r"[\w.-]{1,100}", pname):
        node["package_name"] = pname
    mid = add(node)
    n = 0

    def pep(reqs, scope, group=None):
        nonlocal n
        for r in reqs if isinstance(reqs, list) else []:
            if n >= MAX_DEPS:
                return
            parsed = _pep508(r)
            if parsed:
                key, attrs = parsed
                edge(mid, package("pypi", key, key, rel), "depends_on", scope=scope, group=group, **attrs)
                n += 1

    pep(proj.get("dependencies"), "main")
    opt = proj.get("optional-dependencies")
    for g, reqs in sorted(opt.items()) if isinstance(opt, dict) else []:
        pep(reqs, "optional", g)
    groups = data.get("dependency-groups")
    for g, reqs in sorted(groups.items()) if isinstance(groups, dict) else []:
        pep([r for r in reqs if isinstance(r, str)] if isinstance(reqs, list) else [], "group", g)
    bs = data.get("build-system")
    if isinstance(bs, dict):
        pep(bs.get("requires"), "build")

    def poetry_table(table, scope, group=None):
        nonlocal n
        for name, spec in sorted(table.items()) if isinstance(table, dict) else []:
            if n >= MAX_DEPS:
                return
            if not isinstance(name, str) or name.lower() == "python" or not _PEP508.match(name):
                continue
            attrs: dict = {}
            if isinstance(spec, dict):
                if isinstance(spec.get("path"), str):
                    # a local path dependency is first-party: point at its manifest if it has one
                    target = _join(base, spec["path"])
                    if target is not None:
                        cand = posixpath.join(target, "pyproject.toml") if target else "pyproject.toml"
                        if _is_file(root_res, cand):
                            edge(mid, add(node_for(cand)), "depends_on", scope=scope, group=group)
                            n += 1
                    continue
                for src in ("git", "url"):
                    if src in spec:
                        attrs["origin"] = src
                ver = spec.get("version")
                if spec.get("optional") is True:
                    attrs["optional"] = True
            else:
                ver = spec
            if isinstance(ver, str) and ver.strip() and _PLAIN_VERSION.fullmatch(ver.strip()):
                attrs["specifier"] = ver.strip()
            key = _pypi(name)
            edge(mid, package("pypi", key, key, rel), "depends_on", scope=scope, group=group, **attrs)
            n += 1

    poetry_table(poetry.get("dependencies"), "main")
    poetry_table(poetry.get("dev-dependencies"), "dev")
    pgroups = poetry.get("group")
    for g, tbl in sorted(pgroups.items()) if isinstance(pgroups, dict) else []:
        if isinstance(tbl, dict):
            poetry_table(tbl.get("dependencies"), "group", g)


def _requirements(text, rel, base, mid, root_res, add, edge, package):
    text = re.sub(r"\\\r?\n", " ", text)
    n = 0
    for raw in text.splitlines():
        if n >= MAX_DEPS:
            return
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
        if line.startswith("-"):
            m = re.match(r"^(-r|--requirement|-c|--constraint)(?:\s*=\s*|\s+|(?<=-[rc]))(\S+)$", line)
            if m:
                target = _join(base, m.group(2))
                if target and _is_file(root_res, target):
                    other = node_for(target) or {"id": f"manifest:{target}",
                                                 "label": posixpath.basename(target),
                                                 "type": "requirements", "file_type": "manifest",
                                                 "source_file": target}
                    rel_name = "includes" if m.group(1) in ("-r", "--requirement") else "constrained_by"
                    edge(mid, add(other), rel_name)
                    n += 1
            continue   # every other option (-e, --index-url, --hash, ...) may carry a URL
        line = re.split(r"\s+--?[A-Za-z]", line, maxsplit=1)[0].strip()   # trailing --hash=... options
        if line.startswith((".", "/", "~")) or ("://" in line and not re.match(r"^[\w.-]+\s*(\[[^\]]*\])?\s*@", line)):
            continue
        parsed = _pep508(line)
        if parsed:
            key, attrs = parsed
            edge(mid, package("pypi", key, key, rel), "depends_on", scope="main", **attrs)
            n += 1


def _package_json(text, rel, me, add, edge, package):
    data = json.loads(text)
    if not isinstance(data, dict):
        return
    node = dict(me)
    pname = data.get("name")
    if isinstance(pname, str) and _NPM_NAME.match(pname):
        node["package_name"] = pname
    mid = add(node)
    n = 0
    for section, scope in (("dependencies", "main"), ("devDependencies", "dev"),
                           ("peerDependencies", "peer"), ("optionalDependencies", "optional")):
        deps = data.get(section)
        for name, ver in sorted(deps.items()) if isinstance(deps, dict) else []:
            if n >= MAX_DEPS:
                return
            if not isinstance(name, str) or not _NPM_NAME.match(name):
                continue
            attrs: dict = {}
            if isinstance(ver, str):
                v = ver.strip()
                if v and _PLAIN_VERSION.fullmatch(v):
                    attrs["version"] = v
                elif v.startswith("workspace:"):
                    attrs["origin"] = "workspace"
                elif v.startswith(("file:", "link:")):
                    attrs["origin"] = "local"
                elif v.startswith("npm:"):
                    attrs["origin"] = "alias"
                elif v:
                    attrs["origin"] = "url"   # git+https://, github:, tarball URL: never copied
            edge(mid, package("npm", name, name, rel), "depends_on", scope=scope, **attrs)
            n += 1


def _galaxy_name(s: str) -> tuple[str | None, str | None]:
    """(name, source kind) of a Galaxy entry; for a URL or path only the trailing name survives."""
    s = s.strip()
    if "," in s:                      # legacy "src,version,name"
        parts = [p.strip() for p in s.split(",")]
        if len(parts) >= 3 and _GALAXY_NAME.match(parts[2]):
            return parts[2], "scm"
        s = parts[0]
    if _GALAXY_NAME.match(s):
        return s, None
    tail = re.split(r"[/:]", s.split("?", 1)[0].split("#", 1)[0].rstrip("/"))[-1]
    for suffix in (".git", ".tar.gz", ".tgz"):
        if tail.endswith(suffix):
            tail = tail[: -len(suffix)]
    return (tail, "scm" if "://" in s or s.startswith(("git", "git+")) else "local") \
        if tail and _GALAXY_NAME.match(tail) and "@" not in tail else (None, None)


def galaxy_entries(data) -> list[tuple[str, str, str | None, str | None]] | None:
    """[(kind collection|role, name, plain version or None, origin or None)] of one parsed
    requirements.yml, at most MAX_DEPS; None when the document is some other tool's file."""
    if isinstance(data, list):
        sections = [("role", data)]
    elif isinstance(data, dict) and ({"collections", "roles"} & set(data)):
        sections = [("collection", data.get("collections")), ("role", data.get("roles"))]
    else:
        return None
    out: list[tuple[str, str, str | None, str | None]] = []
    for kind, entries in sections:
        for ent in entries if isinstance(entries, list) else []:
            if len(out) >= MAX_DEPS:
                return out
            ver = None
            if isinstance(ent, str):
                name, source = _galaxy_name(ent)
            elif isinstance(ent, dict):
                raw = ent.get("name") if isinstance(ent.get("name"), str) else ent.get("src")
                if not isinstance(raw, str):
                    continue
                name, source = _galaxy_name(raw)
                if ent.get("type") in ("git", "url", "file", "dir") and source is None:
                    source = "scm" if ent.get("type") in ("git", "url") else "local"
                v = ent.get("version")
                ver = str(v).strip() if isinstance(v, (str, int, float)) else None
            else:
                continue
            if not name:
                continue
            out.append((kind, name, ver if ver and _PLAIN_VERSION.fullmatch(ver) else None, source))
    return out


def _galaxy(text, rel, me, add, edge, package):
    entries = galaxy_entries(yaml.safe_load(text))
    if entries is None:
        return                        # some other tool's requirements.yml
    mid = add(dict(me))
    for kind, name, ver, source in entries:
        edge(mid, package("galaxy", name, name, rel), "depends_on", kind=kind,
             version=ver, origin=source)


_ROLE_SEGMENT = re.compile(r"\w+")


def galaxy_provides(requirements, role_names) -> list[tuple[dict, str]]:
    """Which Galaxy requirement provides which role: [(extpackage node, role name)].

    `requirements` is [(rel, parsed document)] of requirements.yml files, `role_names` the names
    behind role:<name> ids. A role entry provides the role of exactly its name (the `name:` it
    is installed as, else the trailing name of its src); a collection `ns.coll` provides each
    role referenced by its fully qualified name `ns.coll.<role>`. Names only, like the rest of
    this pass; a document that is not a requirements file contributes nothing."""
    names = {r for r in role_names if isinstance(r, str)}
    by_coll: dict[str, list[str]] = {}
    for r in sorted(names):
        head, dot, tail = r.rpartition(".")
        if dot and head.count(".") == 1 and _ROLE_SEGMENT.fullmatch(tail):
            by_coll.setdefault(head, []).append(r)
    out: list[tuple[dict, str]] = []
    seen: set[tuple[str, str]] = set()
    for rel, data in requirements:
        for kind, name, _, _ in galaxy_entries(data) or ():
            if kind == "role":
                targets = [name] if name in names else []
            else:
                targets = by_coll.get(name, [])
            for t in targets:
                if (name, t) not in seen:
                    seen.add((name, t))
                    out.append((package_node("galaxy", name, name, rel), t))
    return out
