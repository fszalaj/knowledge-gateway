"""Ansible/YAML extractor - the relationships a generic AST tool cannot see.

Repo-level pass over one pruned, sorted walk of the tree. It reads:

  filter_plugins/*.py                     filter name -> the Python function implementing it
  <role>/tasks/*.yml, meta/main.yml,      roles, detected by structure (tasks/*.yml or
  handlers/main.yml                       meta/main.yml), wherever they live
  any *.yml / *.yaml playbook             a YAML list whose dict items carry `hosts` or
                                          `import_playbook`, anywhere outside a role's own
                                          tasks/handlers/defaults/vars/meta/templates/files,
                                          group_vars/, host_vars/ and .github/
  INI inventories                         files named `hosts`, `inventory` or `*.ini` that hold
                                          [group] sections (and no `key = value` config lines)
  YAML inventories                        a mapping of groups to {hosts:, children:, vars:}
  group_vars/<group>(.yml|.yaml|.json|/)  which group / host a vars file or directory configures
  host_vars/<host>(...)
  requirements.yml / requirements.yaml    Ansible Galaxy requirements: only the collection and
                                          role names, through extract_manifests' own parser

Emitted node-id prefixes: `playbook:<rel>`, `role:<name>`, `tasksfile:<rel>`,
`task:<rel>:<pos>:<name>` (in a playbook `<pos>` starts with `<play>.<section>.`, since every
play and section restarts at 0), `handler:<name>`, `filter:<name>`, `pyfunc:<rel>:<fn>`,
`template:<rel>`, `file:<rel>`, `invgroup:<name>`, `invhost:<name>`, `vars:<rel>`; a copied or
templated source that another pass graphs (.py, or a tree-sitter extension) is pointed at
by that pass's `module:<rel>` id instead, so the graphs join. A Galaxy requirement that provides a
role is the manifests pass's `extpackage:galaxy:<name>` node, emitted in that pass's shape.

Emitted relations: uses_role, role_depends_on, include_role, include_tasks, notifies,
has_tasks, has_task, calls_filter, implemented_by, imports_playbook (playbook -> playbook),
renders_template (task -> template), copies_file (task -> file), member_of (host -> group),
child_of (group -> parent group, from `:children` / YAML `children:`), configures
(vars -> group/host) and targets (playbook -> group/host named by a play's `hosts:`; INFERRED
when the name is in no inventory of this repo), provides (extpackage:galaxy:<name> -> role,
INFERRED: a Galaxy role entry of the same name, or the collection `ns.coll` of an `ns.coll.<role>`
reference; a role the repository only references becomes a typed role with origin `galaxy`).

Deliberately absent: every VALUE. No variable names or values from group_vars/host_vars,
`[x:vars]` sections, inline INI host variables, play/task `vars:`, `copy: content:`, module
arguments or vault payloads. No template evaluation, no network, no subprocess; a file that
is unreadable, oversized, malformed or a symlink leaving the root is skipped, never fatal.
Host patterns with wildcards, regexes, ranges or Jinja are not resolved, and dynamic
inventory scripts/plugins are not run.
"""
from __future__ import annotations

import ast
import os
import posixpath
import re
from pathlib import Path

import yaml

INCLUDE_TASKS = {"include_tasks", "import_tasks",
                 "ansible.builtin.include_tasks", "ansible.builtin.import_tasks"}
INCLUDE_ROLE = {"include_role", "import_role",
                "ansible.builtin.include_role", "ansible.builtin.import_role"}
IMPORT_PLAYBOOK = {"import_playbook", "ansible.builtin.import_playbook"}
TEMPLATE_MODULES = {"template", "ansible.builtin.template"}
COPY_MODULES = {"copy", "ansible.builtin.copy"}
_FILTER_RE = re.compile(r"\|\s*([a-zA-Z_]\w*)")
PRUNE = {"node_modules", "venv", "__pycache__", "dist", "build",
         "coverage", "htmlcov", "target", "vendor"}

# A role's own directories hold task lists, variables and payloads - never playbooks or
# inventories - and so do group_vars/host_vars and CI workflow definitions.
_ROLE_SUBDIRS = frozenset({"tasks", "handlers", "defaults", "vars", "meta", "templates", "files"})
_NOT_PLAYBOOK_DIRS = _ROLE_SUBDIRS | {"group_vars", "host_vars", ".github"}
_YAML_EXT = (".yml", ".yaml")
_INI_NAMES = {"hosts", "inventory"}
_GALAXY_REQUIREMENTS = {"requirements.yml", "requirements.yaml"}
MAX_BYTES = 2 * 1024 * 1024        # per-file read cap
MAX_TASKS_PER_FILE = 2000          # per-file task-node cap
MAX_TASK_VISITS = 20_000           # per-file tasks walked (nodes or not), aliases included
MAX_TASK_DEPTH = 32                # block/rescue/always nesting
MAX_VALUE_VISITS = 200_000         # per-file values scanned for filter calls
MAX_INVENTORY_ENTRIES = 5000       # per-file group/host cap
MAX_VARS_ENTRIES = 5000            # per group_vars/host_vars directory
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
_INI_SECTION = re.compile(r"^\[([^\]]+)\]\s*$")
_INI_KEYVAL = re.compile(r"^[^\s=\[]+\s*=")      # `key = value`: a config file, not an inventory
_FREEFORM = re.compile(r"(?:^|\s)(src|remote_src)=(\S+)")
_TRUE = {"true", "yes", "on", "1"}


def skip_dir(name: str, extra: frozenset[str] = frozenset(),
             keep: frozenset[str] = frozenset()) -> bool:
    """Directories never worth graphing: hidden (.git, .next, .venv, .graph, caches...),
    known build/vendor output, plus caller-supplied names. `keep` overrides all of it
    for repos with real code in e.g. .github/ or a first-party vendor/."""
    if name in keep:
        return False
    return name.startswith(".") or name in PRUNE or name in extra


# Ansible YAML carries local tags (!vault, !unsafe). SafeLoader rejects them and the whole
# file would vanish; map every local tag to None instead - the payload is never wanted.
# The pure-Python SafeLoader on purpose, never libyaml's CSafeLoader: this pass loads every YAML
# file in the tree, and CSafeLoader overflows the C stack (SIGSEGV, which no except can catch)
# on deeply nested input, while SafeLoader raises a RecursionError that skips the file.
class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _inside(path: Path, root_r: Path) -> Path | None:
    """The real path, when it exists and does not leave the (resolved) root."""
    try:
        real = Path(path).resolve(strict=True)
        real.relative_to(root_r)
        return real
    except (OSError, ValueError, RuntimeError):
        return None


def _read_text(path: Path, root_r: Path) -> str | None:
    real = _inside(path, root_r)
    try:
        if real is None or not real.is_file() or real.stat().st_size > MAX_BYTES:
            return None
        return real.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _safe_load(path: Path, root_r: Path):
    text = _read_text(path, root_r)
    if text is None:
        return None
    try:
        return yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader subclass
    except Exception:
        return None


def _iter_strings(obj, budget: list[int], slot: int = 0):
    """Every string under obj. Iterative, and alias-safe: a YAML alias shares one container
    among many parents (and may contain itself), so each container is visited once, and the
    caller's per-file count of visited values (`budget[slot]`) caps the rest."""
    stack = [obj]
    seen: set[int] = set()
    while stack and budget[slot] > 0:
        budget[slot] -= 1
        o = stack.pop()
        if isinstance(o, str):
            yield o
        elif isinstance(o, (dict, list)):
            if id(o) in seen:
                continue
            seen.add(id(o))
            stack.extend(reversed(list(o.values() if isinstance(o, dict) else o)))


def _templated(s: str) -> bool:
    return "{{" in s or "{%" in s


def _is_playbook(data) -> bool:
    return isinstance(data, list) and any(
        isinstance(p, dict) and ("hosts" in p or any(k in IMPORT_PLAYBOOK for k in p))
        for p in data)


def _is_yaml_inventory(data) -> bool:
    """Top level maps group names to {hosts:, children:, vars:} and nothing else."""
    if not isinstance(data, dict) or not data:
        return False
    found = False
    for k, v in data.items():
        if not isinstance(k, str):
            return False
        if v is None:
            continue
        if not isinstance(v, dict) or not set(v) <= {"hosts", "children", "vars"}:
            return False
        if not all(v.get(x) is None or isinstance(v.get(x), dict) for x in ("hosts", "children")):
            return False
        found = found or "hosts" in v or "children" in v
    return found


def _parse_ini_inventory(text: str):
    """[(kind, name, group)] with kind in group/host/child, or None if not an inventory.
    Only the first token of a host line is read: inline host variables are dropped, and
    `[x:vars]` sections are skipped unread."""
    entries: list[tuple[str, str, str | None]] = []
    section: str | None = None
    mode = "hosts"
    has_section = has_member = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        m = _INI_SECTION.match(line)
        if m:
            gname, _, suffix = m.group(1).strip().partition(":")
            if suffix == "vars" or suffix not in ("", "children") or not _NAME_RE.match(gname):
                mode = "skip"
                continue
            has_section = True
            section, mode = gname, ("children" if suffix else "hosts")
            entries.append(("group", gname, None))
            continue
        if mode == "skip":
            continue
        if _INI_KEYVAL.match(line):
            return None
        tok = line.split()[0]
        if mode == "children":
            if _NAME_RE.match(tok):
                entries.append(("child", tok, section))
                has_member = True
        else:
            host = re.sub(r":\d+$", "", tok)        # host:port
            if host and "=" not in host:
                entries.append(("host", host, section))
                has_member = True
        if len(entries) >= MAX_INVENTORY_ENTRIES:
            break
    return entries if has_section and has_member else None


def _host_names(spec) -> list[str]:
    """Plain group/host names from a play's `hosts:`; patterns that need evaluation are skipped."""
    items = spec if isinstance(spec, list) else [spec]
    out: list[str] = []
    for it in items:
        if not isinstance(it, str) or _templated(it) or "[" in it:
            continue
        for tok in re.split(r"[,:]", it):
            tok = tok.strip()
            if not tok or tok.startswith("!"):     # an exclusion is not a target
                continue
            tok = tok.lstrip("&")
            if _NAME_RE.match(tok) and tok not in out:
                out.append(tok)
    return out


def _module_src(t: dict, keys: set[str]):
    """(src, remote_src) of a template/copy task, from dict args or free-form `k=v`."""
    for k in t:
        if k in keys:
            spec = t[k]
            if spec is None and isinstance(t.get("args"), dict):
                spec = t["args"]
            if isinstance(spec, dict):
                return spec.get("src"), spec.get("remote_src")
            if isinstance(spec, str):
                kv = dict(m.groups() for m in _FREEFORM.finditer(spec))
                return kv.get("src"), kv.get("remote_src")
            return None, None
    return None, None


def extract(root: Path, exclude: frozenset[str] = frozenset(),
            include: frozenset[str] = frozenset()) -> dict:
    root = Path(root)
    try:
        root_r = root.resolve()
    except OSError:
        root_r = root.absolute()
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()

    # One pruned walk feeds every pass below - rglob would traverse .next/node_modules/...
    # before filtering.
    filter_plugin_files: list[Path] = []
    candidate_dirs: list[Path] = []
    yaml_files: list[Path] = []
    ini_files: list[Path] = []
    vars_dirs: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(x for x in dirnames if not skip_dir(x, exclude, include))  # sorted: deterministic output
        d = Path(dirpath)
        candidate_dirs.append(d)
        if d.name == "filter_plugins":
            filter_plugin_files += sorted(d / f for f in filenames if f.endswith(".py"))
        if d != root and d.name in ("group_vars", "host_vars"):
            vars_dirs.append(d)
        if any(p in _NOT_PLAYBOOK_DIRS for p in d.relative_to(root).parts):
            continue  # role internals, vars, CI: never playbooks or inventories
        for f in sorted(filenames):
            if f.startswith("."):
                continue
            low = f.lower()
            if low.endswith(_YAML_EXT):
                yaml_files.append(d / f)
            elif f in _INI_NAMES or low.endswith(".ini"):
                ini_files.append(d / f)

    def node(nid, **attrs):
        cur = nodes.setdefault(nid, {"id": nid})
        cur.update({k: v for k, v in attrs.items() if v is not None})

    def define(nid, **attrs):
        """Like node(), but the first definition wins (a group named in several files keeps
        the file that declared it first)."""
        cur = nodes.setdefault(nid, {"id": nid})
        for k, v in attrs.items():
            if v is not None:
                cur.setdefault(k, v)

    def edge(s, t, relation, confidence="EXTRACTED", score=None):
        if (s, t, relation) in seen_edges:
            return
        seen_edges.add((s, t, relation))
        node(s)
        node(t)
        e = {"source": s, "target": t, "relation": relation, "confidence": confidence}
        if score is not None:
            e["confidence_score"] = score
        edges.append(e)

    def group(name, rel):
        gid = f"invgroup:{name}"
        define(gid, label=name, type="inventory_group", file_type="ansible", source_file=rel)
        return gid

    def host(name, rel):
        hid = f"invhost:{name}"
        define(hid, label=name, type="inventory_host", file_type="ansible", source_file=rel)
        return hid

    # --- filter plugins: name -> function (what `| name` resolves to) ---
    filter_names: set[str] = set()
    for py in filter_plugin_files:
        rel = py.relative_to(root).as_posix()
        text = _read_text(py, root_r)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):  # unparsable plugin: skip the file, not the build
            continue
        for f in tree.body:  # module-level functions only - avoid bogus method ids
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node(f"pyfunc:{rel}:{f.name}", label=f.name, type="function",
                     file_type="python", source_file=rel)
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "filters":
                for d in ast.walk(n):
                    if isinstance(d, ast.Dict):
                        for k, v in zip(d.keys, d.values):
                            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                fname = k.value
                                filter_names.add(fname)
                                node(f"filter:{fname}", label=fname, type="ansible_filter",
                                     file_type="python", source_file=rel)
                                if isinstance(v, ast.Name):
                                    edge(f"filter:{fname}", f"pyfunc:{rel}:{v.id}", "implemented_by")

    # --- inventories (before plays, so `hosts:` can resolve against them) ---
    for ini in ini_files:
        text = _read_text(ini, root_r)
        entries = _parse_ini_inventory(text) if text is not None else None
        if not entries:
            continue
        rel = ini.relative_to(root).as_posix()
        for kind, name, grp in entries:
            if kind == "group":
                group(name, rel)
            elif kind == "child":
                edge(group(name, rel), group(grp, rel), "child_of")
            else:
                hid = host(name, rel)
                if grp:
                    edge(hid, group(grp, rel), "member_of")

    playbooks: list[tuple[Path, list]] = []
    galaxy_reqs: list[tuple[str, object]] = []
    for yf in yaml_files:
        data = _safe_load(yf, root_r)
        if _is_playbook(data):
            playbooks.append((yf, data))
        elif yf.name.lower() in _GALAXY_REQUIREMENTS:
            galaxy_reqs.append((yf.relative_to(root).as_posix(), data))
        elif _is_yaml_inventory(data):
            rel = yf.relative_to(root).as_posix()
            budget = [MAX_INVENTORY_ENTRIES]

            def walk_group(name, body, depth=0, rel=rel, budget=budget):
                gid = group(name, rel)
                if not isinstance(body, dict) or depth > 50 or budget[0] <= 0:
                    return gid
                hosts = body.get("hosts")
                for h in hosts if isinstance(hosts, dict) else ():
                    if isinstance(h, str) and budget[0] > 0:  # the host's vars are never read
                        budget[0] -= 1
                        edge(host(h, rel), gid, "member_of")
                children = body.get("children")
                for c, cb in (children.items() if isinstance(children, dict) else ()):
                    if isinstance(c, str) and budget[0] > 0:
                        budget[0] -= 1
                        edge(walk_group(c, cb, depth + 1), gid, "child_of")
                return gid

            for gname, body in data.items():
                walk_group(gname, body)

    # --- group_vars / host_vars: which group or host a vars file configures (names only) ---
    for vd in vars_dirs:
        is_group = vd.name == "group_vars"
        try:
            entries = sorted(vd.iterdir())[:MAX_VARS_ENTRIES]
        except OSError:
            continue
        for e in entries:
            if e.name.startswith(".") or _inside(e, root_r) is None:
                continue
            if e.is_dir():
                name = e.name
            elif e.suffix.lower() in (".yml", ".yaml", ".json"):
                name = e.stem
            elif e.suffix == "":
                name = e.name
            else:
                continue
            rel = e.relative_to(root).as_posix()
            vid = f"vars:{rel}"
            node(vid, label=e.name, type="group_vars" if is_group else "host_vars",
                 file_type="ansible", source_file=rel)
            edge(vid, group(name, rel) if is_group else host(name, rel), "configures")

    def src_target(src, kind, ctx):
        """Resolve a template/copy `src` the way Ansible searches for it; None if not found."""
        if not isinstance(src, str):
            return None
        src = src.strip()
        if not src or _templated(src) or src.startswith(("/", "~")):
            return None
        where, base = ctx
        base_rel = base.relative_to(root).as_posix()
        sub = "templates" if kind == "template" else "files"
        cands = [f"{base_rel}/{sub}/{src}", f"{base_rel}/{src}"]
        if where == "role":
            cands.append(f"{base_rel}/tasks/{src}")
        for c in cands:
            rel = posixpath.normpath(c)
            if rel == ".." or rel.startswith("../") or rel.startswith("/"):
                continue
            real = _inside(root / rel, root_r)
            if real is None:
                continue
            is_dir = real.is_dir()
            ext = posixpath.splitext(rel)[1].lower()
            graphed = not is_dir and not any(
                skip_dir(p, exclude, include) for p in rel.split("/")[:-1])
            if graphed and (ext == ".py" or ext in _ts_exts()):
                mid = f"module:{rel}"
                define(mid, label=rel, type="module",
                       file_type="python" if ext == ".py" else _ts_exts()[ext], source_file=rel)
                return mid
            if kind == "template" and not is_dir:
                tid = f"template:{rel}"
                define(tid, label=posixpath.basename(rel), type="template",
                       file_type="ansible", source_file=rel)
                return tid
            fid = f"file:{rel}"
            # file_type "file": the shape extract_docker/extract_ghactions give file:<rel> too
            define(fid, label=posixpath.basename(rel), type="directory" if is_dir else "file",
                   file_type="file", source_file=rel)
            return fid
        return None

    def walk_tasks(tasks, owner, rel, ctx, prefix="", budget=None, path=None):
        # budget: [task nodes left, tasks left to walk, values left to scan] for the whole file.
        # Aliases can make a task list contain itself (`- &t {block: [*t]}`) or repeat one
        # block exponentially, so the walk tracks the lists on its current path and caps both
        # its depth and the number of tasks it visits, not just the nodes it emits.
        if budget is None:
            budget = [MAX_TASKS_PER_FILE, MAX_TASK_VISITS, MAX_VALUE_VISITS]
        if path is None:
            path = set()
        if not isinstance(tasks, list) or id(tasks) in path or len(path) >= MAX_TASK_DEPTH:
            return
        path.add(id(tasks))
        try:
            _walk_list(tasks, owner, rel, ctx, prefix, budget, path)
        finally:
            path.discard(id(tasks))

    def _walk_list(tasks, owner, rel, ctx, prefix, budget, path):
        for i, t in enumerate(tasks):
            if not isinstance(t, dict) or budget[0] <= 0 or budget[1] <= 0:
                continue
            budget[1] -= 1
            for blk in ("block", "rescue", "always"):
                if blk in t:
                    # Nested tasks restart at 0, so without the parent's position in the id
                    # a block's first task and the file's first task are one node.
                    walk_tasks(t[blk], owner, rel, ctx, prefix=f"{prefix}{i}.{blk}.",
                               budget=budget, path=path)
            name = t.get("name")
            if not isinstance(name, str):
                name = None
            tid = f"task:{rel}:{prefix}{i}:" + (name or "unnamed")[:40]

            def task_node():
                node(tid, label=name or "unnamed", type="task", file_type="ansible",
                     source_file=rel, source_location=f"#{prefix}{i}")

            for k in t:
                if k in INCLUDE_TASKS:
                    spec = t[k]
                    f = spec.get("file") if isinstance(spec, dict) else spec
                    if isinstance(f, str):
                        # normpath: a `../common.yml` include must reach the real file node
                        target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), f))
                        edge(owner, f"tasksfile:{target}", "include_tasks")
                if k in INCLUDE_ROLE:
                    spec = t[k]
                    rn = spec.get("name") if isinstance(spec, dict) else spec
                    if isinstance(rn, str):
                        edge(owner, f"role:{rn}", "include_role")
            nt = t.get("notify")
            for h in ([nt] if isinstance(nt, str) else nt if isinstance(nt, list) else []):
                if isinstance(h, str):
                    task_node()
                    edge(tid, f"handler:{h}", "notifies")
            # no filter plugin in the repo: nothing to resolve, so nothing to scan
            used = {m for s in _iter_strings(t, budget, 2)
                    for m in _FILTER_RE.findall(s) if m in filter_names} if filter_names else set()
            for fn in sorted(used):                 # sorted: a set's order varies per process
                task_node()
                edge(tid, f"filter:{fn}", "calls_filter")
            src, _ = _module_src(t, TEMPLATE_MODULES)
            target = src_target(src, "template", ctx)
            if target:
                task_node()
                edge(tid, target, "renders_template")
            src, remote = _module_src(t, COPY_MODULES)
            local = remote is None or remote is False or (
                isinstance(remote, str) and remote.strip().lower() not in _TRUE
                and not _templated(remote))
            target = src_target(src, "copy", ctx) if local else None
            if target:
                task_node()
                edge(tid, target, "copies_file")
            if tid in nodes:  # only tasks that point somewhere become nodes
                budget[0] -= 1
                edge(owner, tid, "has_task")

    # --- roles: detect by STRUCTURE, not a fixed roles/<x>/ path, so we also map a bare
    # role (the repo root IS a role), a collection of roles at the repo root, and the
    # standard roles/<x>/ layout. A directory is a role if it has tasks/*.yml or meta/main.yml.
    def _is_role_dir(d: Path) -> bool:
        t = d / "tasks"
        if t.is_dir():
            try:
                if any(t.glob("*.yml")) or any(t.glob("*.yaml")):
                    return True
            except OSError:
                pass
        return (d / "meta" / "main.yml").exists()

    role_dirs: list[Path] = []
    if _is_role_dir(root):
        role_dirs.append(root)
    for d in candidate_dirs:
        # purely structural: a role's own tasks/handlers/meta dir is not itself a role
        # (it has no tasks/*.yml or meta/main.yml), so no name-based exclusion is needed.
        if d != root and _is_role_dir(d):
            role_dirs.append(d)

    for d in dict.fromkeys(role_dirs):
        rname = d.name if d != root else (root.name or "root")
        rrel = "." if d == root else d.relative_to(root).as_posix()
        node(f"role:{rname}", label=rname, type="role", file_type="ansible", source_file=rrel)
        meta = d / "meta" / "main.yml"
        if meta.exists():
            data = _safe_load(meta, root_r) or {}
            if isinstance(data, dict):
                deps = data.get("dependencies")
                for dep in deps if isinstance(deps, list) else []:
                    dn = (dep.get("role") or dep.get("name")) if isinstance(dep, dict) else dep
                    if isinstance(dn, str):
                        edge(f"role:{rname}", f"role:{dn}", "role_depends_on")
        tdir = d / "tasks"
        if tdir.is_dir():
            for tf in sorted(list(tdir.glob("*.yml")) + list(tdir.glob("*.yaml"))):
                rel = tf.relative_to(root).as_posix()
                node(f"tasksfile:{rel}", label=tf.name, type="tasksfile",
                     file_type="ansible", source_file=rel)
                edge(f"role:{rname}", f"tasksfile:{rel}", "has_tasks")
                walk_tasks(_safe_load(tf, root_r), f"tasksfile:{rel}", rel, ("role", d))
        hf = d / "handlers" / "main.yml"
        if hf.exists():
            hs = _safe_load(hf, root_r) or []
            rel = hf.relative_to(root).as_posix()
            for h in hs if isinstance(hs, list) else []:
                if isinstance(h, dict) and isinstance(h.get("name"), str):
                    node(f"handler:{h['name']}", label=h["name"], type="handler",
                         file_type="ansible", source_file=rel)

    # --- playbooks ---
    groups = {n[len("invgroup:"):] for n in nodes if n.startswith("invgroup:")
              and nodes[n].get("type") == "inventory_group"}
    hosts = {n[len("invhost:"):] for n in nodes if n.startswith("invhost:")}
    for pb, plays in playbooks:
        rel = pb.relative_to(root).as_posix()
        pid = f"playbook:{rel}"
        node(pid, label=pb.name, type="playbook", file_type="ansible", source_file=rel)
        budget = [MAX_TASKS_PER_FILE, MAX_TASK_VISITS, MAX_VALUE_VISITS]
        for pi, play in enumerate(plays):
            if not isinstance(play, dict):
                continue
            for k in play:
                v = play[k]
                if k in IMPORT_PLAYBOOK and isinstance(v, str) and v.strip() and not _templated(v):
                    v = v.strip()
                    if v.startswith("/"):
                        continue
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), v))
                    if target != ".." and not target.startswith("../"):
                        edge(pid, f"playbook:{target}", "imports_playbook")
            for h in _host_names(play.get("hosts")):
                if h in groups or h in ("all", "ungrouped"):
                    edge(pid, group(h, rel), "targets")
                elif h in hosts:
                    edge(pid, host(h, rel), "targets")
                elif h != "localhost":  # implicit localhost is not an inventory member
                    edge(pid, group(h, rel), "targets", confidence="INFERRED")
            roles = play.get("roles")
            for r in roles if isinstance(roles, list) else []:
                rn = (r.get("role") or r.get("name")) if isinstance(r, dict) else r
                if isinstance(rn, str):
                    edge(pid, f"role:{rn}", "uses_role")
            # a play's own handlers are notify targets just like a role's handlers/main.yml;
            # without a node here every `notify` of one points at an untyped placeholder
            hs = play.get("handlers")
            for h in hs if isinstance(hs, list) else []:
                if isinstance(h, dict) and isinstance(h.get("name"), str):
                    define(f"handler:{h['name']}", label=h["name"], type="handler",
                           file_type="ansible", source_file=rel)
            for sec in ("tasks", "pre_tasks", "post_tasks", "handlers"):
                # Every play and section restarts at index 0, so the play index and section
                # are part of the id: `task:<rel>:<play>.<section>.<i>:<name>`.
                walk_tasks(play.get(sec) or [], pid, rel, ("play", pb.parent),
                           prefix=f"{pi}.{sec}.", budget=budget)

    # --- Galaxy requirements: a role installed by `ansible-galaxy` is referenced here but
    # defined nowhere in the tree, so without this join it stays an untyped placeholder. Which
    # requirement provides which role is the manifests pass's rule; the roles are known here.
    if galaxy_reqs:
        try:
            from .extract_manifests import galaxy_provides   # lazy: that module imports this one
            provided = galaxy_provides(galaxy_reqs, [n[len("role:"):] for n in nodes
                                                     if n.startswith("role:")])
        except Exception:  # a requirements shape nobody foresaw costs the join, not the pass
            provided = []
        for pkg, rn in provided:
            rid = f"role:{rn}"
            define(pkg["id"], **{k: v for k, v in pkg.items() if k != "id"})
            if not nodes.get(rid, {}).get("type"):
                node(rid, label=rn, type="role", file_type="ansible",
                     source_file=pkg["source_file"], origin="galaxy")
            edge(pkg["id"], rid, "provides", confidence="INFERRED")

    return {"nodes": list(nodes.values()), "edges": edges}


_TS_EXTS: dict[str, str] | None = None


def _ts_exts() -> dict[str, str]:
    """Extension -> language of the tree-sitter pass, so a copied script points at the
    `module:` node that pass emits. Imported lazily: the table, not the parser."""
    global _TS_EXTS
    if _TS_EXTS is None:
        try:
            from .treesitter import EXT_LANG
            _TS_EXTS = dict(EXT_LANG)
        except Exception:  # pragma: no cover
            _TS_EXTS = {}
    return _TS_EXTS
