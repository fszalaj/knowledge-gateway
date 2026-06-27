"""Python extractor (stdlib `ast`, no dependency): module/function/class/method nodes,
import edges, and within-file call edges (INFERRED).

Top-level functions get stable `pyfunc:<rel>:<name>` ids (these are what Ansible filter
plugins and call edges reference); methods are scoped as `pymethod:<rel>:<Class>.<name>`
so same-named functions/methods do not collide.
"""
from __future__ import annotations

import ast
from pathlib import Path

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def extract(path: Path, rel: str, resolver=None) -> dict:
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return {"nodes": [], "edges": []}

    nodes: list[dict] = []
    edges: list[dict] = []
    mod = f"module:{rel}"
    nodes.append({"id": mod, "label": rel, "type": "module", "file_type": "python", "source_file": rel})

    top_funcs: set[str] = set()
    for node in tree.body:  # module-level only -> stable, collision-free ids
        if isinstance(node, _FUNC):
            top_funcs.add(node.name)
            nid = f"pyfunc:{rel}:{node.name}"
            nodes.append({"id": nid, "label": node.name, "type": "function",
                          "file_type": "python", "source_file": rel, "source_location": f"L{node.lineno}"})
            edges.append({"source": mod, "target": nid, "relation": "defines", "confidence": "EXTRACTED"})
        elif isinstance(node, ast.ClassDef):
            cid = f"pyclass:{rel}:{node.name}"
            nodes.append({"id": cid, "label": node.name, "type": "class",
                          "file_type": "python", "source_file": rel, "source_location": f"L{node.lineno}"})
            edges.append({"source": mod, "target": cid, "relation": "defines", "confidence": "EXTRACTED"})
            for m in node.body:
                if isinstance(m, _FUNC):
                    mid = f"pymethod:{rel}:{node.name}.{m.name}"
                    nodes.append({"id": mid, "label": f"{node.name}.{m.name}", "type": "method",
                                  "file_type": "python", "source_file": rel, "source_location": f"L{m.lineno}"})
                    edges.append({"source": cid, "target": mid, "relation": "defines", "confidence": "EXTRACTED"})

    def _imp(target):  # one import edge (module:<rel> first-party, else extmodule:<name>)
        edges.append({"source": mod, "target": target, "relation": "imports", "confidence": "EXTRACTED"})

    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                r = resolver.resolve_py_abs(a.name) if resolver else None
                _imp(f"module:{r}" if r else f"extmodule:{a.name.split('.')[0]}")
        elif isinstance(n, ast.ImportFrom):
            resolved = (resolver.resolve_py_from(rel, n.module, n.level or 0,
                                                 [al.name for al in n.names])
                        if resolver else [])
            if resolved:
                for r in resolved:
                    _imp(f"module:{r}")
            elif n.module:  # unresolved (or no resolver): external top-level, as before
                _imp(f"extmodule:{n.module.split('.')[0]}")

    # within-file calls between top-level functions (INFERRED)
    for fn in [n for n in tree.body if isinstance(n, _FUNC)]:
        caller = f"pyfunc:{rel}:{fn.name}"
        for c in ast.walk(fn):
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id in top_funcs and c.func.id != fn.name):
                edges.append({"source": caller, "target": f"pyfunc:{rel}:{c.func.id}",
                              "relation": "calls", "confidence": "INFERRED", "confidence_score": 0.85})
    return {"nodes": nodes, "edges": edges}
