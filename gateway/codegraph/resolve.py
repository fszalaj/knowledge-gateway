"""Resolve first-party import specifiers to ``module:<rel>`` file nodes.

Without this, every import points at a phantom ``extmodule:<name>`` node, so import
edges never reach the real first-party file: ``god_nodes`` degree then tracks file
size instead of fan-in, and ``graph_shortest_path`` between first-party modules routes
through shared phantom nodes (noise). This maps a specifier to a first-party file
rel-path *within the graphed root*:

  - Python: absolute dotted (``pkg.mod[.submod]``) and relative (``from . / .. import``),
    honoring the graphed-root package anchor (``root.name``) and from-import submodules.
  - TS/JS: relative (``./`` ``../``), ``tsconfig.json`` ``paths`` / ``baseUrl`` aliases, and
    extensionless / ``.js``->``.ts(x)`` / ``index`` resolution.

Specifiers that do not resolve to a graphed file fall back to ``extmodule`` (external /
third-party), exactly as before. Pure stdlib (no networkx) so it imports without the
[graph] extra. See wiki: "code-graph import resolution".
"""
from __future__ import annotations

import json
import posixpath as pp

# TS/JS module-resolution extension order (extensionless + .js->.ts(x) + index files).
_TS_EXTS = (".ts", ".tsx", ".d.ts", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".json")
_TS_INDEX_EXTS = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs")
# an explicit .js-family specifier may point at its .ts source (TS / NodeNext)
_JS_TO_TS = ((".js", (".ts", ".tsx")), (".jsx", (".tsx",)), (".mjs", (".mts",)), (".cjs", (".cts",)))
TS_LANGS = {"javascript", "typescript", "tsx"}  # langs whose specifiers we resolve
_MAX_TSCONFIG_UP = 4  # search the root + up to N parents for a tsconfig/jsconfig


def _skip_ws_comments(text: str, j: int, n: int) -> int:
    """Index of the next significant char at/after j, skipping whitespace and // /*..*/ comments."""
    while j < n:
        ch = text[j]
        if ch in " \t\r\n":
            j += 1
        elif ch == "/" and j + 1 < n and text[j + 1] == "/":
            j += 2
            while j < n and text[j] != "\n":
                j += 1
        elif ch == "/" and j + 1 < n and text[j + 1] == "*":
            j += 2
            while j + 1 < n and not (text[j] == "*" and text[j + 1] == "/"):
                j += 1
            j += 2
        else:
            break
    return j


def _strip_jsonc(text: str) -> str:
    """Make a tsconfig parseable by json: drop // and /*..*/ comments and trailing commas.

    A string-aware char scanner (not a regex): // and /* inside string values such as
    "@/*", "./src/*" or "**/*.ts" are kept (they appear in every Next.js tsconfig); a comma
    is dropped only when the next significant token is } or ] - so a comma inside a string,
    or one followed by a comment then a brace, is handled correctly.
    """
    out: list[str] = []
    i, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:  # keep the escaped char verbatim
                out.append(text[i + 1]); i += 2; continue
            if c == '"':
                in_str = False
            i += 1; continue
        if c == '"':
            in_str = True; out.append(c); i += 1; continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":      # line comment
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":      # block comment
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c == ",":
            j = _skip_ws_comments(text, i + 1, n)
            if j < n and text[j] in "}]":
                i += 1; continue                               # trailing comma -> drop
            out.append(c); i += 1; continue
        out.append(c); i += 1
    return "".join(out)


class ImportResolver:
    """Maps import specifiers to first-party ``module:<rel>`` nodes; built once per graph.

    `root` is the (absolute) graphed source dir; `rels` are the posix rel-paths of every
    file that the build will actually parse (so a resolved target always has a real node).
    """

    def __init__(self, root, rels):
        self._root = pp.normpath(str(root).replace("\\", "/"))
        self.rels = set(rels)
        self._anchor = pp.basename(self._root)
        self._py = self._build_py_index()
        self._ts_paths, self._ts_path_base, self._ts_baseurl = self._load_tsconfig()

    # ---------------------------------------------------------------- Python
    def _build_py_index(self) -> dict:
        """dotted module path -> rel, under both the root and root.name (parent-on-path)."""
        idx: dict[str, str] = {}
        for rel in sorted(self.rels):  # sorted -> deterministic when foo.py and foo/__init__.py coexist
            if not rel.endswith(".py"):
                continue
            parts = rel.split("/")
            dotted = ".".join(parts[:-1] if parts[-1] == "__init__.py"
                               else parts[:-1] + [parts[-1][:-3]])
            if not dotted:
                continue
            idx.setdefault(dotted, rel)
            idx.setdefault(f"{self._anchor}.{dotted}", rel)
        return idx

    def resolve_py_abs(self, dotted: str | None) -> str | None:
        """`import a.b.c` -> rel of that module/package, or None if not first-party."""
        return self._py.get(dotted) if dotted else None

    def _py_file(self, base: str) -> str | None:
        base = base.strip("/")
        if not base:
            return None
        if base + ".py" in self.rels:
            return base + ".py"
        if base + "/__init__.py" in self.rels:
            return base + "/__init__.py"
        return None

    def resolve_py_from(self, importer_rel: str, module: str | None, level: int,
                        names: list[str]) -> list[str]:
        """`from [.|..]module import n1, n2` -> rels of first-party submodules/package.

        A submodule (``module.ni`` is itself a file) is preferred so the edge is precise;
        symbols that are not submodules attach one edge to the package/module instead.
        """
        real = [n for n in (names or []) if n and n != "*"]
        resolved: list[str] = []
        if level and level > 0:  # relative: walk up from the importer's package dir
            base = pp.dirname(importer_rel)
            for _ in range(level - 1):
                if not base:  # walked above the graphed root -> cannot resolve
                    return []
                base = pp.dirname(base)
            target = pp.normpath(pp.join(base, module.replace(".", "/"))) if module else base
            if target == ".":
                target = ""
            pkg = self._py_file(target)
            for nm in real:
                sm = self._py_file(pp.join(target, nm) if target else nm)
                if sm:
                    resolved.append(sm)
            if pkg and pkg not in resolved and (not real or len(resolved) < len(real)):
                resolved.append(pkg)
        elif module:  # absolute
            for nm in real:
                r = self.resolve_py_abs(f"{module}.{nm}")
                if r:
                    resolved.append(r)
            pkg = self.resolve_py_abs(module)
            if pkg and pkg not in resolved and (not real or len(resolved) < len(real)):
                resolved.append(pkg)
        return list(dict.fromkeys(resolved))

    # ----------------------------------------------------------------- TS/JS
    def _load_tsconfig(self):
        """Nearest tsconfig/jsconfig at root..parents -> (paths, paths-base-abs, baseUrl-abs).

        baseUrl-abs is None when baseUrl is absent (so bare specifiers are not resolved
        against it - only explicit `paths` aliases and relative imports are).
        """
        d = self._root
        for _ in range(_MAX_TSCONFIG_UP + 1):
            for name in ("tsconfig.json", "jsconfig.json"):
                try:
                    with open(pp.join(d, name), encoding="utf-8", errors="replace") as fh:
                        cfg = json.loads(_strip_jsonc(fh.read()))
                except (OSError, ValueError):
                    cfg = None
                if isinstance(cfg, dict):  # tolerate type-wrong configs (don't abort the build)
                    co = cfg.get("compilerOptions")
                    co = co if isinstance(co, dict) else {}
                    baseurl = co.get("baseUrl")
                    baseurl = baseurl if isinstance(baseurl, str) else None
                    paths = co.get("paths")
                    paths = paths if isinstance(paths, dict) else {}
                    base = pp.normpath(pp.join(d, baseurl)) if baseurl else d
                    return paths, base, (base if baseurl else None)
            nd = pp.dirname(d)
            if nd == d:
                break
            d = nd
        return {}, self._root, None

    def _probe(self, cand_abs: str) -> str | None:
        """An absolute candidate path (maybe extensionless) -> a rel in `rels`, or None."""
        rel = pp.relpath(cand_abs, self._root)
        if rel == ".." or rel.startswith("../"):  # escapes the graphed root -> external
            return None
        rel = "" if rel == "." else rel
        for jsext, tsexts in _JS_TO_TS:  # a .js specifier prefers its .ts source (TS resolves ./x.js -> ./x.ts)
            if rel.endswith(jsext):
                for te in tsexts:
                    if rel[: -len(jsext)] + te in self.rels:
                        return rel[: -len(jsext)] + te
                break  # only one jsext can match
        if rel in self.rels:
            return rel
        for e in _TS_EXTS:
            if rel + e in self.rels:
                return rel + e
        for e in _TS_INDEX_EXTS:
            if pp.join(rel, "index" + e) in self.rels:
                return pp.join(rel, "index" + e)
        return None

    def _alias_targets(self, spec: str):
        for pat, tgts in self._ts_paths.items():
            if isinstance(tgts, str):
                tgts = [tgts]
            elif not isinstance(tgts, (list, tuple)):
                continue
            tgts = [t for t in tgts if isinstance(t, str)]
            if pat.endswith("/*"):
                pre = pat[:-1]  # "@/*" -> "@/"
                if spec.startswith(pre):
                    tail = spec[len(pre):]
                    for t in tgts:
                        sub = (t[:-1] + tail) if t.endswith("*") else t
                        yield pp.normpath(pp.join(self._ts_path_base, sub))
            elif pat == spec:
                for t in tgts:
                    yield pp.normpath(pp.join(self._ts_path_base, t))

    def resolve_ts(self, importer_rel: str, spec: str) -> str | None:
        """A JS/TS import specifier -> first-party rel, or None (external/unresolved)."""
        spec = (spec or "").strip()
        if not spec or "://" in spec or spec.startswith(("data:", "node:", "#")):
            return None
        if spec.startswith("."):  # relative to the importer
            base = pp.dirname(pp.join(self._root, importer_rel))
            return self._probe(pp.normpath(pp.join(base, spec)))
        for cand in self._alias_targets(spec):  # tsconfig paths alias
            hit = self._probe(cand)
            if hit:
                return hit
        if self._ts_baseurl is not None:  # non-relative baseUrl resolution
            return self._probe(pp.normpath(pp.join(self._ts_baseurl, spec)))
        return None
