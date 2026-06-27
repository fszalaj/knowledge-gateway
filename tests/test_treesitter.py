"""Broad-language tree-sitter pass (the [graph-all] extra).

Regression guard: this pass was once silently broken (it threw on every file and the
error was swallowed -> zero nodes). These tests fail loudly if extraction stops working.
Skipped only if tree-sitter-language-pack is not installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_language_pack")

from gateway.codegraph import treesitter as ts


def _w(p, s):
    p.write_text(s, encoding="utf-8")
    return p


def test_javascript_defs_calls_imports(tmp_path):
    f = ts.extract(_w(tmp_path / "a.js",
        "import x from 'react';\n"
        "function helper(){ return 1; }\n"
        "function main(){ return helper(); }\n"
        "class C { go(){ return this.n(); } n(){ return 2; } }\n"), "a.js")
    labels = {n["label"] for n in f["nodes"]}
    rels = {e["relation"] for e in f["edges"]}
    assert {"helper", "main", "C", "go", "n"} <= labels        # functions + class + methods
    assert {"defines", "imports", "calls"} <= rels
    assert any(e["target"] == "extmodule:react" for e in f["edges"])
    assert any(e["relation"] == "calls" for e in f["edges"])   # main -> helper resolved in-file


def test_go_rust_terraform(tmp_path):
    fg = ts.extract(_w(tmp_path / "m.go",
        "package main\nfunc Foo() int { return Bar() }\nfunc Bar() int { return 1 }\ntype T struct{}\n"), "m.go")
    assert {"Foo", "Bar", "T"} <= {n["label"] for n in fg["nodes"]}

    fr = ts.extract(_w(tmp_path / "m.rs",
        "use std::io;\nfn foo() -> i32 { bar() }\nfn bar() -> i32 { 1 }\nstruct S;\ntrait Tr {}\n"), "m.rs")
    types = {n["type"] for n in fr["nodes"]}
    assert "function" in types and ("struct" in types or "trait" in types)

    ft = ts.extract(_w(tmp_path / "main.tf",
        'resource "aws_instance" "web" { ami = "x" }\nvariable "region" {}\n'), "main.tf")
    assert any(n["type"] == "resource" for n in ft["nodes"])


def test_unknown_extension_is_empty(tmp_path):
    assert ts.extract(_w(tmp_path / "x.unknownext", "stuff"), "x.unknownext") == {"nodes": [], "edges": []}
