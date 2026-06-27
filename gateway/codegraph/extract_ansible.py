"""Ansible/YAML extractor - the relationships a generic AST tool cannot see.

Repo-level pass: walks playbooks, roles (tasks/handlers/meta), and filter_plugins,
emitting nodes for playbooks/plays/roles/tasksfiles/tasks/handlers/filters and edges
for uses_role, role_depends_on, include_role, include_tasks, notifies, has_tasks,
calls_filter (Ansible task -> Python filter plugin), and implemented_by (filter -> fn).
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import yaml

INCLUDE_TASKS = {"include_tasks", "import_tasks",
                 "ansible.builtin.include_tasks", "ansible.builtin.import_tasks"}
INCLUDE_ROLE = {"include_role", "import_role",
                "ansible.builtin.include_role", "ansible.builtin.import_role"}
_FILTER_RE = re.compile(r"\|\s*([a-zA-Z_]\w*)")
PRUNE = {".git", "node_modules", ".venv", "venv", "__pycache__", ".graph",
         "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox"}


def _pruned(p, root) -> bool:
    return any(part in PRUNE for part in p.relative_to(root).parts)


def _safe_load(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


def extract(root: Path) -> dict:
    root = Path(root)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def node(nid, **attrs):
        cur = nodes.setdefault(nid, {"id": nid})
        cur.update({k: v for k, v in attrs.items() if v is not None})

    def edge(s, t, relation, confidence="EXTRACTED", score=None):
        node(s)
        node(t)
        e = {"source": s, "target": t, "relation": relation, "confidence": confidence}
        if score is not None:
            e["confidence_score"] = score
        edges.append(e)

    # --- filter plugins: name -> function (what `| name` resolves to) ---
    filter_names: set[str] = set()
    for py in root.rglob("filter_plugins/*.py"):
        if _pruned(py, root):
            continue
        rel = py.relative_to(root).as_posix()
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
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

    def walk_tasks(tasks, owner, rel):
        if not isinstance(tasks, list):
            return
        for i, t in enumerate(tasks):
            if not isinstance(t, dict):
                continue
            for blk in ("block", "rescue", "always"):
                if blk in t:
                    walk_tasks(t[blk], owner, rel)
            name = t.get("name")
            tid = f"task:{rel}:{i}:" + (name or "unnamed")[:40]
            interesting = False
            for k in t:
                if k in INCLUDE_TASKS:
                    spec = t[k]
                    f = spec.get("file") if isinstance(spec, dict) else spec
                    if isinstance(f, str):
                        edge(owner, f"tasksfile:{(Path(rel).parent / f).as_posix()}", "include_tasks")
                        interesting = True
                if k in INCLUDE_ROLE:
                    spec = t[k]
                    rn = spec.get("name") if isinstance(spec, dict) else spec
                    if isinstance(rn, str):
                        edge(owner, f"role:{rn}", "include_role")
                        interesting = True
            nt = t.get("notify")
            for h in ([nt] if isinstance(nt, str) else nt or []):
                if isinstance(h, str):
                    node(tid, label=name or "unnamed", type="task", file_type="ansible",
                         source_file=rel, source_location=f"#{i}")
                    edge(tid, f"handler:{h}", "notifies")
                    interesting = True
            used = {m for s in _iter_strings(t) for m in _FILTER_RE.findall(s) if m in filter_names}
            for fn in used:
                node(tid, label=name or "unnamed", type="task", file_type="ansible",
                     source_file=rel, source_location=f"#{i}")
                edge(tid, f"filter:{fn}", "calls_filter")
            if (interesting or used) and tid in nodes:
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
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [x for x in dirnames if x not in PRUNE]  # prune in place
        d = Path(dirpath)
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
            data = _safe_load(meta) or {}
            if isinstance(data, dict):
                for dep in (data.get("dependencies") or []):
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
                walk_tasks(_safe_load(tf), f"tasksfile:{rel}", rel)
        hf = d / "handlers" / "main.yml"
        if hf.exists():
            hs = _safe_load(hf) or []
            rel = hf.relative_to(root).as_posix()
            for h in hs if isinstance(hs, list) else []:
                if isinstance(h, dict) and h.get("name"):
                    node(f"handler:{h['name']}", label=h["name"], type="handler",
                         file_type="ansible", source_file=rel)

    for pb in list(root.glob("playbook_*.yml")) + list(root.glob("*.yml")):
        plays = _safe_load(pb)
        if not isinstance(plays, list) or not any(
            isinstance(p, dict) and ("hosts" in p or "roles" in p or "tasks" in p) for p in plays
        ):
            continue  # not a playbook
        rel = pb.relative_to(root).as_posix()
        node(f"playbook:{rel}", label=pb.name, type="playbook", file_type="ansible", source_file=rel)
        for play in plays:
            if not isinstance(play, dict):
                continue
            for r in (play.get("roles") or []):
                rn = (r.get("role") or r.get("name")) if isinstance(r, dict) else r
                if isinstance(rn, str):
                    edge(f"playbook:{rel}", f"role:{rn}", "uses_role")
            for sec in ("tasks", "pre_tasks", "post_tasks", "handlers"):
                walk_tasks(play.get(sec) or [], f"playbook:{rel}", rel)

    return {"nodes": list(nodes.values()), "edges": edges}
