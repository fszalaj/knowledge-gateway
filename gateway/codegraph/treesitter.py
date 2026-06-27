"""Broad multi-language pass via tree-sitter-language-pack (the [graph-all] extra).

One dependency covers ~30+ languages. For each parsed file we emit:
  - a `module:` node + a node per definition (function / method / class / struct /
    interface / trait / enum / type / namespace / object / HCL block),
  - `defines` containment edges (module -> def, class -> method, ...),
  - `imports` edges (module -> `extmodule:<name>`), capturing framework/library use,
  - within-file `calls` edges (INFERRED) when a call resolves to a def in the same file.

Definitions are matched by tree-sitter node kind (a data table - add a language by adding
its node kinds), so coverage grows without rewrites. AST-only: no LLM, no network.

If the package is not installed, AVAILABLE is False and the build skips these languages
(the Python `ast` and Ansible `yaml` passes need no tree-sitter).
"""
from __future__ import annotations

from pathlib import Path

from .resolve import TS_LANGS

try:  # the broad pass is opt-in; the core install stays light
    from tree_sitter_language_pack import get_parser
    AVAILABLE = True
except Exception:  # pragma: no cover - exercised only with the [graph-all] extra
    AVAILABLE = False

# ---------------------------------------------------------------------------
# extension -> tree-sitter language name. Add an extension/language here; it is
# data, not code. (.py is handled by the ast pass; .yml/.yaml by the Ansible pass.)
EXT_LANG = {
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".go": "go", ".rs": "rust", ".java": "java", ".cs": "csharp",
    ".scala": "scala", ".sc": "scala", ".kt": "kotlin", ".kts": "kotlin", ".swift": "swift",
    ".rb": "ruby", ".php": "php", ".phtml": "php",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".ps1": "powershell", ".psm1": "powershell", ".psd1": "powershell",
    ".tf": "hcl", ".tfvars": "hcl", ".hcl": "hcl",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".lua": "lua", ".r": "r", ".R": "r", ".pl": "perl", ".pm": "perl",
    ".ex": "elixir", ".exs": "elixir", ".clj": "clojure", ".cljs": "clojure",
    ".dart": "dart", ".sql": "sql", ".groovy": "groovy", ".gvy": "groovy",
    ".jl": "julia", ".sol": "solidity", ".hs": "haskell", ".ml": "ocaml", ".mli": "ocaml",
}
LANG_EXTS = set(EXT_LANG)
_HCL = {"hcl", "terraform"}
_MAX_NODES = 8000     # cap definitions emitted from one (e.g. minified/vendored) file
_MAX_VISIT = 400_000  # cap AST nodes visited per file (bounds traversal regardless of def count)

# node kind -> our node type. Kinds are largely unique across grammars; "block" is
# intentionally excluded here (it is a code block in most languages) and handled only
# for HCL below.
_DEFS = {
    # functions / methods
    "function_declaration": "function", "function_definition": "function", "function_item": "function",
    "function_expression": "function", "arrow_function": "function", "function_statement": "function",
    "generator_function_declaration": "function", "func_literal": "function", "fn_item": "function",
    "method_definition": "method", "method_declaration": "method", "method_spec": "method",
    "constructor_declaration": "method", "method": "method", "singleton_method": "method",
    "subroutine": "function", "lambda": "function", "macro_definition": "function",
    "anonymous_function": "function",
    # classes / types / interfaces / modules
    "class_declaration": "class", "class_definition": "class", "class_specifier": "class",
    "class": "class", "record_declaration": "class", "actor_declaration": "class",
    "struct_item": "struct", "struct_specifier": "struct", "struct_declaration": "struct",
    "interface_declaration": "interface", "interface_type": "interface",
    "annotation_type_declaration": "interface", "protocol_declaration": "interface",
    "trait_item": "trait", "trait_declaration": "trait",
    "impl_item": "impl", "extension_declaration": "impl",
    "enum_declaration": "enum", "enum_item": "enum", "enum_specifier": "enum",
    "type_declaration": "type", "type_alias_declaration": "type", "type_spec": "type",
    "type_definition": "type", "type_alias": "type", "union_specifier": "type",
    "namespace_definition": "namespace", "namespace_declaration": "namespace",
    "module": "module", "mod_item": "module", "object_declaration": "object",
}
_IMPORTS = {
    "import_statement", "import_declaration", "import_spec", "import_from_statement",
    "use_declaration", "using_directive", "preproc_include", "namespace_use_declaration",
    "extern_crate_declaration",
}
# anonymous function kinds: their first identifier child is a parameter, not a name
_ANON = {"arrow_function", "function_expression", "anonymous_function", "lambda", "func_literal"}
_CALLS = {
    "call_expression", "invocation_expression", "method_invocation", "call",
    "function_call_expression", "member_call_expression", "macro_invocation",
    "method_call", "new_expression", "command",
}
_NAME_KINDS = {
    "identifier", "type_identifier", "field_identifier", "constant", "property_identifier",
    "name", "word", "scoped_identifier", "simple_identifier", "constant_identifier",
    "command_name",
}
_STRING_KINDS = {
    "string", "string_literal", "interpreted_string_literal", "string_lit", "string_fragment",
    "raw_string_literal", "system_lib_string", "quoted_template",
}

# ---------------------------------------------------------------------------
# Portable accessors: the bundled binding exposes node members as methods
# (node.kind(), node.child(i), tree.root_node()); upstream py-tree-sitter exposes
# them as properties (node.type, node.children, tree.root_node). Handle both.
def _v(x):
    return x() if callable(x) else x


def _root(tree):
    r = tree.root_node
    return r() if callable(r) else r


def _kind(n) -> str:
    for a in ("type", "kind"):
        if hasattr(n, a):
            return _v(getattr(n, a)) or ""
    return ""


def _children(n):
    if hasattr(n, "children"):
        c = _v(n.children)
        if c is not None:
            return list(c)
    cnt = _v(getattr(n, "child_count", 0)) or 0
    return [n.child(i) for i in range(cnt)]


def _field(n, name):
    f = getattr(n, "child_by_field_name", None)
    try:
        return f(name) if f else None
    except Exception:
        return None


def _parent(n):
    return _v(getattr(n, "parent", None))


def _text(n, src: bytes) -> str:
    return src[_v(n.start_byte):_v(n.end_byte)].decode("utf-8", "replace")


def _row(n) -> int:
    for a in ("start_point", "start_position"):
        if hasattr(n, a):
            sp = _v(getattr(n, a))
            if hasattr(sp, "row"):
                return sp.row
            try:
                return sp[0]
            except Exception:
                pass
    return 0


def _clean(s: str) -> str:
    return s.strip().strip('"').strip("'").strip("`").strip("<>").strip()[:120]


_PARSERS: dict = {}


def _parser(lang):
    if lang not in _PARSERS:
        _PARSERS[lang] = get_parser(lang)
    return _PARSERS[lang]


def _def_name(n, src):
    fn = _field(n, "name")
    if fn:
        return _clean(_text(fn, src))
    # anonymous function assigned to a name: const Foo = () => {} / { key: fn }
    p = _parent(n)
    if p:
        for fld in ("name", "key", "left"):
            pf = _field(p, fld)
            if pf:
                return _clean(_text(pf, src))
    if _kind(n) in _ANON:  # an anonymous function's first identifier is a parameter, not a name
        return None
    for c in _children(n):  # fallback: first identifier-ish child (named defs only)
        if _kind(c) in _NAME_KINDS:
            return _clean(_text(c, src))
    return None


def _hcl_name(n, src):
    parts = []
    for c in _children(n):
        k = _kind(c)
        if k in _NAME_KINDS or k in _STRING_KINDS:
            t = _clean(_text(c, src))
            if t:
                parts.append(t)
        if len(parts) >= 3:
            break
    return ".".join(parts) or None


def _import_target(n, src):
    """Best-effort module name(s) referenced by an import node (iterative, bounded)."""
    out = []
    stack = [n]
    while stack and len(out) < 8:
        x = stack.pop()
        k = _kind(x)
        if k in _STRING_KINDS or k in ("scoped_identifier", "dotted_name", "namespace_name",
                                       "qualified_name", "package_identifier", "crate"):
            s = _clean(_text(x, src))
            if s:
                out.append(s)
            continue  # a module path is a leaf for our purposes; do not descend further
        stack.extend(_children(x))
    return list(dict.fromkeys(out))[:3]


def _callee_name(n, src):
    target = (_field(n, "function") or _field(n, "method") or _field(n, "constructor")
              or _field(n, "name"))
    if target is None:
        kids = _children(n)
        target = kids[0] if kids else None
    if target is None:
        return None
    last = None  # rightmost identifier (handles member access a.b.helper -> helper)
    stack = [target]
    while stack:
        x = stack.pop()
        if _kind(x) in _NAME_KINDS:
            last = x
        stack.extend(reversed(_children(x)))
    return _clean(_text(last, src)) if last else None


def extract(path: Path, rel: str, resolver=None) -> dict:
    if not AVAILABLE:
        return {"nodes": [], "edges": []}
    lang = EXT_LANG.get(path.suffix.lower())
    if not lang:
        return {"nodes": [], "edges": []}
    try:
        parser = _parser(lang)
        raw = path.read_bytes()
        text = raw.decode("utf-8", "replace")
        try:
            tree = parser.parse(text)          # bundled binding wants str
        except TypeError:
            tree = parser.parse(raw)           # upstream py-tree-sitter wants bytes
    except Exception:
        return {"nodes": [], "edges": []}

    src = raw
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    defs_by_name: dict[str, str] = {}
    calls: list[tuple[str, str]] = []
    mod = f"module:{rel}"
    nodes.append({"id": mod, "label": rel, "type": "module", "file_type": lang, "source_file": rel})

    def add_def(n, kind, container):
        nm = _hcl_name(n, src) if kind == "block" else _def_name(n, src)
        if not nm:
            return container
        line = _row(n) + 1
        nid = f"{lang}:{rel}:{nm}#L{line}"
        if nid not in seen:
            seen.add(nid)
            nodes.append({"id": nid, "label": nm, "type": "resource" if kind == "block" else kind,
                          "file_type": lang, "source_file": rel, "source_location": f"L{line}"})
            edges.append({"source": container, "target": nid, "relation": "defines",
                          "confidence": "EXTRACTED"})
            defs_by_name.setdefault(nm, nid)
        return nid

    stack = [(_root(tree), mod)]
    visited = 0
    while stack and len(nodes) < _MAX_NODES and visited < _MAX_VISIT:
        visited += 1
        n, container = stack.pop()
        k = _kind(n)
        nxt = container
        if k in _DEFS:
            nxt = add_def(n, _DEFS[k], container)
        elif k == "block" and lang in _HCL:
            nxt = add_def(n, "block", container)
        elif k in _IMPORTS:
            for m in _import_target(n, src):
                r = resolver.resolve_ts(rel, m) if (resolver and lang in TS_LANGS) else None
                edges.append({"source": mod, "target": f"module:{r}" if r else f"extmodule:{m}",
                              "relation": "imports", "confidence": "EXTRACTED"})
        elif k in _CALLS:
            nm = _callee_name(n, src)
            if nm:
                calls.append((container, nm))
        for c in _children(n):
            stack.append((c, nxt))

    for caller, nm in calls:  # resolve within-file calls only
        tgt = defs_by_name.get(nm)
        if tgt and tgt != caller:
            edges.append({"source": caller, "target": tgt, "relation": "calls",
                          "confidence": "INFERRED", "confidence_score": 0.8})

    # de-dup extmodule import edges
    uniq, ek = [], set()
    for e in edges:
        key = (e["source"], e["target"], e["relation"])
        if key in ek:
            continue
        ek.add(key)
        uniq.append(e)
    return {"nodes": nodes, "edges": uniq}
