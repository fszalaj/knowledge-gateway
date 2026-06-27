"""Import-resolution tests: first-party imports must reach module:<rel> nodes,
external ones must stay extmodule:<name>. Covers Python (relative/absolute/submodule
+ root-name anchor) and TS/JS (tsconfig paths alias, relative, extensionless)."""
from __future__ import annotations

import pytest

pytest.importorskip("networkx")

from gateway.codegraph import build_graph
from gateway.codegraph.resolve import ImportResolver


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _imports(data):
    return {(l["source"], l["target"]) for l in data["links"] if l.get("relation") == "imports"}


def test_python_import_resolution(tmp_path):
    anchor = tmp_path.name
    _write(tmp_path / "pkg/__init__.py", "")
    _write(tmp_path / "pkg/b.py", "def thing():\n    return 1\n")
    _write(tmp_path / "pkg/c.py", "other = 2\n")
    _write(tmp_path / "pkg/a.py",
           "import os\n"                      # external -> extmodule
           "from .b import thing\n"           # relative submodule -> pkg/b.py
           "from pkg.c import other\n")       # absolute (subdir package) -> pkg/c.py
    _write(tmp_path / "sub/mod.py",
           f"from {anchor}.pkg.b import thing\n")  # root-name anchor -> pkg/b.py

    imp = _imports(build_graph(tmp_path))
    assert ("module:pkg/a.py", "module:pkg/b.py") in imp        # relative resolved
    assert ("module:pkg/a.py", "module:pkg/c.py") in imp        # absolute resolved
    assert ("module:pkg/a.py", "extmodule:os") in imp           # external untouched
    assert ("module:sub/mod.py", "module:pkg/b.py") in imp      # parent-on-path anchor
    # no first-party import should leak to a phantom extmodule node
    assert not any(t == "extmodule:pkg" or t == "extmodule:b" or t == "extmodule:c"
                   for _, t in imp)


def test_python_resolver_unit(tmp_path):
    r = ImportResolver(tmp_path, ["pkg/__init__.py", "pkg/b.py", "pkg/sub/d.py"])
    assert r.resolve_py_abs("pkg.b") == "pkg/b.py"
    assert r.resolve_py_abs("pkg") == "pkg/__init__.py"
    assert r.resolve_py_abs("nope.x") is None
    # from pkg.sub import d  (d is a submodule file)
    assert r.resolve_py_from("pkg/b.py", "pkg.sub", 0, ["d"]) == ["pkg/sub/d.py"]
    # relative: from . import b  (inside pkg)
    assert r.resolve_py_from("pkg/b.py", None, 1, ["b"]) == ["pkg/b.py"]
    # relative import that climbs above the graphed root must not resolve
    assert r.resolve_py_from("a.py", None, 3, ["x"]) == []


def test_ts_import_resolution(tmp_path):
    pytest.importorskip("tree_sitter_language_pack")
    _write(tmp_path / "tsconfig.json",
           '{\n  "compilerOptions": {\n    "baseUrl": ".",\n'
           '    "paths": { "@/*": ["./src/*"] }  // alias\n  }\n}\n')
    _write(tmp_path / "src/lib/db.ts", "export const db = 1;\n")
    _write(tmp_path / "src/lib/util.ts", "export const u = 2;\n")
    _write(tmp_path / "src/pages/home.ts",
           'import { db } from "@/lib/db";\n'        # tsconfig alias -> src/lib/db.ts
           'import { u } from "../lib/util";\n'      # relative + extensionless -> src/lib/util.ts
           'import React from "react";\n')           # external -> extmodule

    imp = _imports(build_graph(tmp_path))
    assert ("module:src/pages/home.ts", "module:src/lib/db.ts") in imp     # alias resolved
    assert ("module:src/pages/home.ts", "module:src/lib/util.ts") in imp   # relative resolved
    assert ("module:src/pages/home.ts", "extmodule:react") in imp          # external untouched
    assert not any(t == "extmodule:@/lib/db" or t == "extmodule:../lib/util" for _, t in imp)


def test_strip_jsonc_preserves_strings_with_slashes():
    # regression: // and /* inside strings (@/*, ./src/*, **/*.ts) must survive; only
    # real comments + trailing commas go. A naive regex spans @/* .. **/ and eats paths.
    import json

    from gateway.codegraph.resolve import _strip_jsonc
    raw = (
        '{\n'
        '  // line comment\n'
        '  "compilerOptions": {\n'
        '    "baseUrl": ".",\n'
        '    "paths": { "@/*": ["./src/*"] }  /* block comment */\n'
        '  },\n'
        '  "include": ["**/*.ts", "**/*.tsx",]\n'
        '}\n'
    )
    cfg = json.loads(_strip_jsonc(raw))
    assert cfg["compilerOptions"]["paths"] == {"@/*": ["./src/*"]}
    assert cfg["include"] == ["**/*.ts", "**/*.tsx"]


def test_ts_resolver_unit(tmp_path):
    pytest.importorskip("tree_sitter_language_pack")
    _write(tmp_path / "tsconfig.json", '{"compilerOptions":{"baseUrl":".","paths":{"@/*":["./src/*"]}}}')
    # legacy.js is a compiled sibling next to the legacy.ts source
    rels = ["src/lib/db.ts", "src/lib/legacy.ts", "src/lib/legacy.js", "src/pages/home.tsx", "src/ui/index.ts"]
    r = ImportResolver(tmp_path, rels)
    assert r.resolve_ts("src/pages/home.tsx", "@/lib/db") == "src/lib/db.ts"
    assert r.resolve_ts("src/pages/home.tsx", "../ui") == "src/ui/index.ts"   # index resolution
    assert r.resolve_ts("src/pages/home.tsx", "../lib/db.js") == "src/lib/db.ts"  # .js -> .ts source
    # a .js specifier must prefer the .ts source even when a compiled .js sibling exists
    assert r.resolve_ts("src/pages/home.tsx", "../lib/legacy.js") == "src/lib/legacy.ts"
    assert r.resolve_ts("src/pages/home.tsx", "react") is None               # external
    assert r.resolve_ts("src/pages/home.tsx", "../nope") is None


def test_tsconfig_bad_types_do_not_crash(tmp_path):
    # a type-wrong tsconfig must yield no aliases, not abort the build
    _write(tmp_path / "tsconfig.json", '{"compilerOptions": ["nope"], "paths": 5}')
    r = ImportResolver(tmp_path, ["src/a.ts"])
    assert r.resolve_ts("src/a.ts", "@/x") is None


def test_strip_jsonc_trailing_comma_and_string_safety():
    import json

    from gateway.codegraph.resolve import _strip_jsonc
    assert json.loads(_strip_jsonc('{"a":1, // c\n}')) == {"a": 1}        # comma, comment, brace
    assert json.loads(_strip_jsonc('{"a":1, /* c */ }')) == {"a": 1}      # comma, block comment, brace
    assert json.loads(_strip_jsonc('{"a":"x,}","b":2}')) == {"a": "x,}", "b": 2}  # comma in string kept
