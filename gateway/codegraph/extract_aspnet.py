"""ASP.NET Core extractor - the HTTP surface a C# service exposes, and which clients call it.

The tree-sitter pass sees a controller as a class with methods; that `[HttpGet("{id}")]` under
`[Route("api/[controller]")]` is the endpoint `GET /api/orders/{}` lives in attribute arguments it
does not interpret, and a minimal API (`app.MapGet(...)`) is just a call. This repo-level pass
text-scans C# itself (stdlib only, no tree-sitter, no compiler) with a linear tokenizer that
drops comments, preprocessor lines and the contents of non-literal strings, then reads:

  *.cs   controllers: classes with [ApiController] / [Controller], named *Controller, or deriving
         from Controller / ControllerBase (a base named *Controller); [Route] on the class
         (several allowed, inherited from a base class in the repository), [Area] and [ApiVersion]
         literals, and on public methods [HttpGet|HttpPost|HttpPut|HttpPatch|HttpDelete|HttpHead|
         HttpOptions("...")], [Route("...")], [AcceptVerbs(...)], [MapToApiVersion], [NonAction];
         [Authorize] / [AllowAnonymous] on either. Route tokens [controller], [action] (the `Async`
         suffix dropped, as ASP.NET Core does) and [area] are replaced; a template starting with
         `/` or `~/` overrides the class route. A `v{version:apiVersion}` segment becomes `v<n>`
         for each literal major version declared, `{}` otherwise.
         A tuple return type (`public (int, string) Pair()`) is read as a type; [ApiVersion(2.0)]
         and [MapToApiVersion(2)] take a number as well as a string.
         minimal APIs: <x>.MapGet / MapPost / MapPut / MapPatch / MapDelete / MapMethods / Map /
         MapHealthChecks with a literal pattern, below MapGroup("...") prefixes held in a variable
         or chained (at most _MAX_GROUP_DEPTH deep and MAX_ROUTE long; a longer prefix is not
         literal), and RequireAuthorization() / AllowAnonymous() on the endpoint or its group
         (or [Authorize] / [AllowAnonymous] on the handler lambda). `app.Map("/x", b => b.Run(..))`
         - IApplicationBuilder.Map, a middleware branch - is not an endpoint.
         A template argument is literal when it is a string literal, a `+` concatenation of
         literals, nameof(...), or a `const string` declared anywhere in the scanned tree with
         one unambiguous value.
  *.csproj  only located (never parsed here): the nearest enclosing project directory owns a .cs file.

Emitted nodes:
  endpoint:<METHOD> <route_shape>   type endpoint; http_method (GET... or ANY for a route without
                                    a verb), route (the shape), kind controller|minimal,
                                    controller / action (class and method names; action is the
                                    handler method group for a minimal API), auth required|anonymous
                                    when an [Authorize]/[AllowAnonymous]/RequireAuthorization hint
                                    is present, optional_segments (how many trailing segments -
                                    `{id?}`, `{id=1}`, `{*rest}` - a request may omit), source_file,
                                    source_location L<n>. Two handlers of one shape share the node;
                                    the first in walk order sets the attrs.
  aspnetapp:<project dir>           the project that serves the endpoints ("." at the repo root);
                                    only when a *.csproj encloses the handler file
  module:<rel>                      the handler's .cs file, in the exact shape the tree-sitter pass
                                    writes, so the target is typed under [graph-slim] too
  project:<rel>                     the enclosing .csproj, in the manifests pass's shape
Emitted edges:
  handled_by    endpoint -> module:<rel>                       EXTRACTED
  exposes       aspnetapp -> endpoint                          EXTRACTED
  defined_by    aspnetapp -> project:<csproj rel>              EXTRACTED

Route shape: the same rule as the Appsmith pass, so the two meet on one string. A leading `/`,
segments joined by `/`, no trailing slash; every parameter (`{id}`, `{id:int}`, `{**slug}`,
`{id?}`) and every literal segment that is not a plain word (letters, `-` or `_`, at most 24
characters, or a `v<n>` version) becomes `{}`.

Cross-pass links: `link_calls(nodes)` (below) is a post-merge hook, not part of `extract`. Given
every node of the merged graph it returns INFERRED `calls_api` edges from HTTP clients
(`appsmithquery:*` and `httprequest:*` nodes carrying `http_method` / `method` and a
`route_shape` / `path` shape)
to the endpoint whose METHOD and route shape match (with or without its optional trailing
segments). It reads only node attributes, adds no nodes,
and is deterministic, so every pass stays independent of every other.

Deliberately absent - a hard rule: no values. No string contents other than route templates, and
those only as their shape; no [Authorize] policy or role names, no header names or values, no
request bodies, no constant values, no configuration (appsettings*.json is not read), no URLs or
hosts. Conventional routes (MapControllerRoute patterns), actions without any route or verb
attribute, actions inherited from a base controller, non-literal templates, interpolated strings,
Razor Pages, gRPC, SignalR hubs and Azure Functions are not graphed. bin/ and obj/ are pruned. A
file that is oversized, unreadable, a link out of the repository, or that trips the scanner is
skipped whole; nothing is evaluated, compiled or run.
"""
from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path

from .extract_ansible import skip_dir
from .extract_appsmith import _PLAIN_SEGMENT
from .extract_manifests import node_for as _manifest_node

MAX_BYTES = 2 * 1024 * 1024     # a larger .cs file is generated, not authored
MAX_ENDPOINTS_PER_FILE = 2000
MAX_NODES = 50_000              # endpoint nodes per repository
MAX_SELECTORS = 64              # routes one action may expand to (class routes x verbs x versions)
MAX_CONSTS = 50_000             # const string declarations remembered per repository
MAX_STR = 1024                  # longer string literals are never kept
MAX_ROUTE = 512
MAX_LINK_TARGETS = 4            # a client matching more endpoints than this is ambiguous: no edge
MAX_LINK_COMPARISONS = 2_000_000

_DOTNET_OUTPUT = {"bin", "obj"}
_VERB_ATTRS = {"HttpGet": "GET", "HttpPost": "POST", "HttpPut": "PUT", "HttpPatch": "PATCH",
               "HttpDelete": "DELETE", "HttpHead": "HEAD", "HttpOptions": "OPTIONS"}
_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
_MAP_CALLS = {"MapGet": "GET", "MapPost": "POST", "MapPut": "PUT", "MapPatch": "PATCH",
              "MapDelete": "DELETE", "MapMethods": None, "Map": "ANY", "MapHealthChecks": "ANY"}
_TYPE_KW = {"class", "record", "struct", "interface"}
_TWO = {"=>", "==", "!=", "<=", ">=", "+=", "-=", "??", "?.", "::", "&&", "||"}
_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = {")": "(", "]": "[", "}": "{"}
_ESC = {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}
_ROUTE_TOKEN = re.compile(r"\[([A-Za-z]{1,32})\]")
_VERSION = re.compile(r"(\d{1,2})(?:\.0)?")
_VERSION_ATTRS = {"ApiVersion", "MapToApiVersion"}
_MAX_GROUP_DEPTH = 32           # nested MapGroup prefixes followed; a deeper group is not literal
_RELEVANT = ("Route", "Http", "Map", "Controller")
_AMBIGUOUS = object()
_CLIENT_PREFIXES = ("appsmithquery:", "httprequest:")
_CLIENT_PATH_KEYS = ("route_shape", "path", "path_shape", "route", "url_path")


# --------------------------------------------------------------------------------------------
# tokenizer: (kind, value, line); kind is id | str | istr | num | chr | p (punctuation)

def _string(text: str, i: int):
    """(kind, value, end) of the string literal starting at i, or None when none starts there.
    Every branch advances with str.find or one character at a time: linear, never backtracking.
    kind is "str" for a literal with a known value, "istr" for an interpolated or unterminated one."""
    n = len(text)
    j, interp, verb = i, False, False
    while j < n and j - i < 3 and text[j] in "$@":
        interp, verb = interp or text[j] == "$", verb or text[j] == "@"
        j += 1
    if j >= n or text[j] != '"':
        return None
    q = 0
    while j + q < n and text[j + q] == '"':
        q += 1
    if q >= 3 and not verb:                                     # raw string literal
        k = text.find('"' * q, j + q)
        if k < 0:
            return ("istr", None, n)
        end = k + q
        while end < n and text[end] == '"':
            end += 1
        val = text[j + q:k]
        return ("istr", None, end) if interp or len(val) > MAX_STR else ("str", val, end)
    if q == 2:
        return ("str", "", j + 2)
    out: list[str] = []
    k, depth, holes, size = j + 1, 0, False, 0
    while k < n:
        ch = text[k]
        if depth == 0:
            if ch == '"':
                if verb and k + 1 < n and text[k + 1] == '"':
                    k += 2
                    ch = '"'
                else:
                    keep = not holes and size <= MAX_STR
                    return ("str" if keep else "istr", "".join(out) if keep else None, k + 1)
            elif ch == "\\" and not verb:
                ch = _ESC.get(text[k + 1], text[k + 1]) if k + 1 < n else ""
                k += 2
            elif ch == "\n" and not verb:
                return ("istr", None, k)                           # unterminated
            elif interp and ch == "{":
                if k + 1 < n and text[k + 1] == "{":
                    k += 2
                else:
                    depth, holes = 1, True
                    k += 1
                    continue
            elif interp and ch == "}" and k + 1 < n and text[k + 1] == "}":
                k += 2
            else:
                k += 1
            if size <= MAX_STR:
                out.append(ch)
                size += 1
        else:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == '"':                                         # a string inside a hole
                m = k + 1
                while m < n and text[m] not in '"\n':
                    m += 2 if text[m] == "\\" else 1
                k = m + 1
                continue
            elif ch == "\n" and not verb:
                return ("istr", None, k)
            k += 1
    return ("istr", None, n)


def _tokenize(text: str) -> list[tuple]:
    toks: list[tuple] = []
    i, n, line, bol = 0, len(text), 1, True
    while i < n:
        c = text[i]
        if c == "\n":
            line, bol, i = line + 1, True, i + 1
            continue
        if c in " \t\r\f\v﻿":
            i += 1
            continue
        if c == "#" and bol:                                        # preprocessor line
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        bol = False
        if c == "/" and i + 1 < n and text[i + 1] in "/*":
            if text[i + 1] == "/":
                j = text.find("\n", i)
                i = n if j < 0 else j
            else:
                j = text.find("*/", i + 2)
                end = n if j < 0 else j + 2
                line += text.count("\n", i, end)
                i = end
            continue
        if c in '$@"':
            r = _string(text, i)
            if r is not None:
                kind, val, end = r
                toks.append((kind, val, line))
                line += text.count("\n", i, end)
                i = end
                continue
        if c == "'":
            j = i + (3 if text[i + 1:i + 2] == "\\" else 2)
            k = text.find("'", j, j + 10)
            if k >= 0 and "\n" not in text[i:k]:
                toks.append(("chr", None, line))
                i = k + 1
                continue
            toks.append(("p", "'", line))
            i += 1
            continue
        if c.isalpha() or c == "_" or (c == "@" and i + 1 < n and (text[i + 1].isalpha() or text[i + 1] == "_")):
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            toks.append(("id", text[i:j].lstrip("@"), line))
            i = j
            continue
        if c.isdigit():
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "._"):
                j += 1
            # the text is kept only for short numbers: [ApiVersion(2.0)] reads it, nothing else
            toks.append(("num", text[i:j] if j - i <= 16 else None, line))
            i = j
            continue
        two = text[i:i + 2]
        if two in _TWO:
            toks.append(("p", two, line))
            i += 2
            continue
        toks.append(("p", c, line))
        i += 1
    return toks


def _pairs(toks: list[tuple]) -> list[int]:
    """Index of the matching bracket for every ( [ { ) ] }, -1 when unmatched. One pass with a
    stack; a stray closer looks at most 8 levels down, so unbalanced input stays linear."""
    match = [-1] * len(toks)
    stack: list[int] = []
    for idx, (k, v, _) in enumerate(toks):
        if k != "p":
            continue
        if v in _OPEN:
            stack.append(idx)
        elif v in _CLOSE:
            want = _CLOSE[v]
            d = len(stack) - 1
            floor = max(-1, d - 8)
            while d > floor and toks[stack[d]][1] != want:
                d -= 1
            if d > floor:
                o = stack[d]
                del stack[d:]
                match[o], match[idx] = idx, o
    return match


def _is(toks, j, v) -> bool:
    return 0 <= j < len(toks) and toks[j][0] == "p" and toks[j][1] == v


def _skip_angles(toks, j):
    """Index after the generic argument list `<...>` at j, or None when it is not one."""
    depth = 0
    for m in range(j, min(len(toks), j + 128)):
        k, v, _ = toks[m]
        if k == "p" and v == "<":
            depth += 1
        elif k == "p" and v == ">":
            depth -= 1
            if depth == 0:
                return m + 1
        elif not (k == "id" or (k == "p" and v in (",", ".", "?", "[", "]", "(", ")"))):
            return None
    return None


def _split_args(toks, a, b, match):
    """(start, end) of each top-level comma-separated argument in toks[a:b]."""
    out, s, j = [], a, a
    while j < b:
        k, v, _ = toks[j]
        if k == "p" and v in _OPEN and match[j] > j:
            j = match[j] + 1
            continue
        if k == "p" and v == ",":
            out.append((s, j))
            s = j + 1
        j += 1
    if s < b:
        out.append((s, b))
    return out


def _const(consts: dict, qual: str | None, name: str):
    for key in ((f"{qual}.{name}", name) if qual else (name,)):
        v = consts.get(key)
        if isinstance(v, str):
            return v
        if v is _AMBIGUOUS:
            return None
    return None


def _literal(seq, consts: dict):
    """The value of a compile-time string expression: literals, `+`, nameof(...) and const
    references; None for anything else."""
    parts: list[str] = []
    i, n, expect = 0, len(seq), True
    while i < n:
        k, v = seq[i][0], seq[i][1]
        if expect:
            if k == "str" and v is not None:
                parts.append(v)
                i += 1
            elif k == "id" and v == "nameof" and i + 1 < n and seq[i + 1][:2] == ("p", "("):
                j, last = i + 2, None
                while j < n and seq[j][:2] != ("p", ")"):
                    if seq[j][0] == "id":
                        last = seq[j][1]
                    elif seq[j][:2] != ("p", "."):
                        return None
                    j += 1
                if j >= n or last is None:
                    return None
                parts.append(last)
                i = j + 1
            elif k == "id":
                j, qual, last = i, None, v
                while j + 2 < n and seq[j + 1][:2] == ("p", ".") and seq[j + 2][0] == "id":
                    qual, last = last, seq[j + 2][1]
                    j += 2
                val = _const(consts, qual, last)
                if val is None:
                    return None
                parts.append(val)
                i = j + 1
            else:
                return None
            expect = False
        elif k == "p" and v == "+":
            expect = True
            i += 1
        else:
            return None
    if expect:
        return None
    s = "".join(parts)
    return s if len(s) <= MAX_STR else None


# --------------------------------------------------------------------------------------------
# const string collection (phase 1, every .cs file)

def _collect_consts(toks, raw: dict, budget: list):
    """`const string NAME = <expr>;` -> raw[key] = [expr token slices], keyed NAME and Type.NAME."""
    types: list[tuple[str, int]] = []
    pending, depth = None, 0
    n = len(toks)
    for i in range(n):
        k, v, _ = toks[i]
        if k == "p":
            if v == "{":
                depth += 1
                if pending:
                    types.append((pending, depth))
                    pending = None
            elif v == "}":
                if types and types[-1][1] == depth:
                    types.pop()
                depth = max(0, depth - 1)
            elif v == ";":
                pending = None
            continue
        if k != "id":
            continue
        if v in _TYPE_KW and i + 1 < n and toks[i + 1][0] == "id" and not (
                i and toks[i - 1][0] == "p" and toks[i - 1][1] in (".", ":", ",")):
            nxt = toks[i + 1][1]
            pending = toks[i + 2][1] if nxt in ("class", "struct") and i + 2 < n and toks[i + 2][0] == "id" else nxt
            continue
        if v != "const" or i + 2 >= n or toks[i + 1][0] != "id" or toks[i + 1][1] not in ("string", "String"):
            continue
        j = i + 2
        while j + 1 < n and toks[j][0] == "id" and _is(toks, j + 1, "="):
            name = toks[j][1]
            e = j + 2
            while e < n and e - j < 66 and not (toks[e][0] == "p" and toks[e][1] in (",", ";")):
                e += 1
            if budget[0] <= 0:
                return
            budget[0] -= 1
            expr = [(t[0], t[1]) for t in toks[j + 2:e]]
            keys = [name] + ([f"{types[-1][0]}.{name}"] if types else [])
            for key in keys:
                raw.setdefault(key, []).append(expr)
            if not _is(toks, e, ","):
                break
            j = e + 1


def _resolve_consts(raw: dict) -> dict:
    consts: dict = {}
    for _ in range(4):                       # consts built from consts: a few rounds suffice
        changed = False
        for key in sorted(raw):
            if key in consts:
                continue
            vals = [_literal(e, consts) for e in raw[key]]
            if any(v is None for v in vals):
                continue
            uniq = set(vals)
            consts[key] = vals[0] if len(uniq) == 1 else _AMBIGUOUS
            changed = True
        if not changed:
            break
    for key in raw:                          # unresolved names never fall back to anything
        consts.setdefault(key, _AMBIGUOUS)
    return consts


# --------------------------------------------------------------------------------------------
# controllers

def _attributes(toks, a, b, match, consts) -> list[dict]:
    """[{name, pos: [value|None], named: {lower name: value|None}}] for the list inside [ ]."""
    out = []
    i = a
    if i + 1 < b and toks[i][0] == "id" and _is(toks, i + 1, ":"):
        i += 2                                                      # [return: X], [assembly: X]
    while i < b:
        j = i
        if toks[i][0] == "id":
            name = toks[i][1]
            j = i + 1
            while j + 1 < b and _is(toks, j, ".") and toks[j + 1][0] == "id":
                name = toks[j + 1][1]
                j += 2
            if j < b and _is(toks, j, "<"):
                j = _skip_angles(toks, j) or j
            if name.endswith("Attribute") and len(name) > 9:
                name = name[:-9]
            pos, named = [], {}
            if j < b and _is(toks, j, "(") and match[j] > j:
                close = match[j]
                for s, e in _split_args(toks, j + 1, close, match):
                    if e - s >= 2 and toks[s][0] == "id" and toks[s + 1][0] == "p" and toks[s + 1][1] in ("=", ":"):
                        named[toks[s][1].lower()] = _literal(toks[s + 2:e], consts)
                    elif e - s == 1 and toks[s][0] == "num" and name in _VERSION_ATTRS:
                        pos.append(toks[s][1])                      # [ApiVersion(2.0)]
                    else:
                        pos.append(_literal(toks[s:e], consts))
                j = close + 1
            out.append({"name": name, "pos": pos, "named": named})
        while j < b and not _is(toks, j, ","):
            j = match[j] + 1 if toks[j][0] == "p" and toks[j][1] in _OPEN and match[j] > j else j + 1
        i = j + 1
    return out


def _bases(toks, j, match):
    """(base type names, index of the `{` or `;` that ends the type header)."""
    n = len(toks)
    limit = min(n, j + 512)
    bases: list[str] = []
    if _is(toks, j, "<"):
        j = _skip_angles(toks, j) or j
    if _is(toks, j, "(") and match[j] > j:
        j = match[j] + 1                                            # primary constructor
    if _is(toks, j, ":"):
        j += 1
        cur = None
        while j < limit:
            k, v, _ = toks[j]
            if k == "p" and v in ("{", ";"):
                break
            if k == "id" and v == "where":
                break
            if k == "p" and v == "<":
                j2 = _skip_angles(toks, j)
                if j2 is None:
                    break
                j = j2
                continue
            if k == "p" and v == "(" and match[j] > j:
                j = match[j] + 1
                continue
            if k == "id":
                cur = v
            elif k == "p" and v == ",":
                if cur:
                    bases.append(cur)
                cur = None
            j += 1
        if cur:
            bases.append(cur)
    while j < limit and not (toks[j][0] == "p" and toks[j][1] in ("{", ";")):
        j = match[j] + 1 if toks[j][0] == "p" and toks[j][1] == "(" and match[j] > j else j + 1
    return bases, j


def _tuple_type(toks, j, match) -> bool:
    """Whether the `(` at j opens a tuple type in a member declaration: the balanced `( )` is
    followed by the member name (an identifier other than `where`), or by `>` `,` `?` `[` inside
    or after a generic type (`Task<(int, string)>`). A method's own parameter list is followed
    by `{`, `=>`, `;`, `:` or `where` instead."""
    c = match[j] if 0 <= j < len(toks) else -1
    if c <= j or c + 1 >= len(toks):
        return False
    k, v, _ = toks[c + 1]
    return (k == "id" and v != "where") or (k == "p" and v in (">", ",", "?", "["))


def _scan_classes(toks, match, consts) -> list[dict]:
    classes: list[dict] = []
    stack: list[dict] = []
    depth = 0
    pending_attrs: list = []
    mods: list[str] = []
    clean = True
    prev = None
    pending = None
    i, n = 0, len(toks)
    while i < n:
        k, v, ln = toks[i]
        if k == "p":
            if v == "[" and prev in (None, "{", "}", ";", "]") and match[i] > i:
                pending_attrs.extend(_attributes(toks, i + 1, match[i], match, consts))
                i, prev = match[i] + 1, "]"
                continue
            if v == "{":
                depth += 1
                if pending is not None:
                    pending["depth"] = depth
                    stack.append(pending)
                    pending = None
                pending_attrs, mods, clean = [], [], True
            elif v == "}":
                if stack and stack[-1]["depth"] == depth:
                    stack.pop()
                depth = max(0, depth - 1)
                pending_attrs, mods, clean = [], [], True
            elif v == ";":
                pending = None
                pending_attrs, mods, clean = [], [], True
            elif (v == "(" and clean and stack and depth == stack[-1]["depth"]
                    and _tuple_type(toks, i, match)):
                i, prev = match[i] + 1, ")"                         # `(int, string) Pair()`
                continue
            elif v in ("=", "=>", "("):
                clean = False
            prev = v
            i += 1
            continue
        if k == "id":
            if v in _TYPE_KW and prev not in (".", ":", ",") and i + 1 < n:
                j = i + 1
                if v == "record" and toks[j][0] == "id" and toks[j][1] in ("class", "struct"):
                    j += 1
                if j < n and toks[j][0] == "id":
                    bases, end = _bases(toks, j + 1, match)
                    info = {"name": toks[j][1], "attrs": pending_attrs, "mods": mods, "bases": bases,
                            "line": ln, "methods": [], "type": v, "depth": -1}
                    if v in ("class", "record"):
                        classes.append(info)
                    pending = info
                    pending_attrs, mods, clean = [], [], False
                    i, prev = end, "class"
                    continue
            if stack and depth == stack[-1]["depth"] and clean and prev != ".":
                j = i + 1
                if _is(toks, j, "<"):
                    j = _skip_angles(toks, j)
                if j == i + 1 and _is(toks, j, "(") and _tuple_type(toks, j, match):
                    # `public (int, string) Pair()`: a modifier, then a tuple return type
                    mods.append(v)
                    i, prev = match[j] + 1, ")"
                    continue
                if j is not None and _is(toks, j, "(") and match[j] > j:
                    if v != stack[-1]["name"]:
                        stack[-1]["methods"].append({"name": v, "attrs": pending_attrs,
                                                     "mods": list(mods), "line": ln})
                    pending_attrs, clean = [], False
                    i, prev = match[j] + 1, ")"
                    continue
            if clean:
                mods.append(v)
        prev = v if k == "id" else k
        i += 1
    return classes


def _is_controller(c: dict) -> bool:
    names = {a["name"] for a in c["attrs"]}
    if "NonController" in names or {"abstract", "static"} & set(c["mods"]) or c["type"] != "class":
        return False
    return bool({"ApiController", "Controller"} & names or c["name"].endswith("Controller")
                or any(b.endswith(("Controller", "ControllerBase")) for b in c["bases"]))


def _strings(attrs, name, cap=8) -> list:
    return [p for a in attrs if a["name"] == name for p in a["pos"][:1]][:cap]


def _class_meta(c: dict, class_map: dict):
    """(route templates (None = not literal), versions, auth, area) of a controller, following
    base classes for an inherited [Route] (AttributeUsage Inherited=true)."""
    routes, versions, auth, area = None, None, None, None
    cur, hops, seen = c, 0, set()
    while cur is not None and hops < 8 and id(cur) not in seen:
        seen.add(id(cur))
        names = {a["name"] for a in cur["attrs"]}
        if routes is None and "Route" in names:
            routes = _strings(cur["attrs"], "Route", MAX_SELECTORS)
        if versions is None and "ApiVersion" in names:
            versions = _strings(cur["attrs"], "ApiVersion")
        if auth is None:
            auth = "anonymous" if "AllowAnonymous" in names else "required" if "Authorize" in names else None
        if area is None and "Area" in names:
            area = (_strings(cur["attrs"], "Area") or [None])[0] or ""
        nxt = next((class_map[b] for b in cur["bases"] if b in class_map), None)
        cur, hops = nxt, hops + 1
    return routes or [], versions or [], auth, area


def _selectors(m: dict) -> list[tuple]:
    """(verb, template) per route the action answers; template None = not literal, "" for an
    attribute without a template (the class route alone)."""
    with_tpl, verbs_only, routes = [], [], []
    for a in m["attrs"]:
        nm = a["name"]
        if nm in _VERB_ATTRS:
            if a["pos"]:
                with_tpl.append((_VERB_ATTRS[nm], a["pos"][0]))
            elif "template" in a["named"]:
                with_tpl.append((_VERB_ATTRS[nm], a["named"]["template"]))
            else:
                verbs_only.append(_VERB_ATTRS[nm])
        elif nm == "Route":
            routes.append(a["pos"][0] if a["pos"] else a["named"].get("template"))
        elif nm == "AcceptVerbs":
            vs = [p.upper() for p in a["pos"] if isinstance(p, str) and p.upper() in _VERBS]
            if "route" in a["named"]:
                with_tpl += [(x, a["named"]["route"]) for x in vs]
            else:
                verbs_only += vs
    sel = list(with_tpl)
    for r in routes:
        sel += [(verb, r) for verb in (verbs_only or ["ANY"])]
    if not routes:
        sel += [(verb, "") for verb in verbs_only]
    return list(dict.fromkeys(sel))[:MAX_SELECTORS]


def _combine(prefix: str, tpl: str) -> str:
    if not prefix:
        return tpl
    if not tpl:
        return prefix
    return prefix.rstrip("/") + "/" + tpl.lstrip("/")


def _split_route(t: str) -> list[str]:
    """Segments of a route template, `/` inside a `{...}` parameter (a regex constraint) kept."""
    segs, cur, depth = [], [], 0
    for ch in t:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch == "/" and depth == 0:
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    segs.append("".join(cur))
    return segs


def route_shape(template, version: str | None = None) -> str | None:
    """`/a/{}/b` shape of a route template (see the module docstring); `version` is the major
    version a `v{version:apiVersion}` segment stands for."""
    if not isinstance(template, str) or len(template) > MAX_STR:
        return None
    t = template.strip()
    if t.startswith("~"):
        t = t[1:]
    out = []
    for seg in _split_route(t):
        seg = seg.strip()
        if not seg:
            continue
        if "{" in seg or "}" in seg:
            low = seg.lower()
            if version and ":apiversion" in low and seg.endswith("}") and seg.index("{") <= 1:
                seg = seg[:seg.index("{")] + version
                out.append(seg if _PLAIN_SEGMENT.fullmatch(seg) else "{}")
            else:
                out.append("{}")
        elif _PLAIN_SEGMENT.fullmatch(seg):
            out.append(seg)
        else:
            out.append("{}")
    shape = "/" + "/".join(out)
    return shape if len(shape) <= MAX_ROUTE else None


def _optional_tail(template) -> int:
    """How many trailing segments of a route template a request may leave out: optional
    parameters (`{id?}`, `{id:int?}`), parameters with a default (`{id=1}`) and a catch-all
    (`{*rest}` / `{**rest}`). The route shape still writes them as `{}`."""
    if not isinstance(template, str) or len(template) > MAX_STR:
        return 0
    k = 0
    for seg in reversed([s.strip() for s in _split_route(template.strip().lstrip("~")) if s.strip()]):
        inner = seg[1:-1].strip() if seg.startswith("{") and seg.endswith("}") else None
        if not inner or not (inner.startswith("*") or inner.endswith("?")
                             or ("=" in inner and "(" not in inner)):
            break
        k += 1
    return k


def _controller_endpoints(c: dict, class_map: dict):
    """(verb, shape, method, auth, line, optional tail) for every routed public action of c."""
    class_routes, class_versions, class_auth, area = _class_meta(c, class_map)
    literal_routes = [r for r in class_routes if isinstance(r, str)]
    ctrl = c["name"][:-10] if c["name"].endswith("Controller") and len(c["name"]) > 10 else c["name"]
    out = []
    for m in c["methods"]:
        names = {a["name"] for a in m["attrs"]}
        if "public" not in m["mods"] or "static" in m["mods"] or "NonAction" in names:
            continue
        action = m["name"][:-5] if m["name"].endswith("Async") and len(m["name"]) > 5 else m["name"]
        auth = ("anonymous" if "AllowAnonymous" in names else "required" if "Authorize" in names
                else class_auth)
        versions = _strings(m["attrs"], "MapToApiVersion") or class_versions
        majors = []
        for v in versions:
            mm = _VERSION.fullmatch(v) if isinstance(v, str) else None
            majors.append(mm.group(1) if mm else None)
        majors = list(dict.fromkeys(majors)) or [None]
        seen = set()
        for verb, tpl in _selectors(m):
            if tpl is None:
                continue                                            # not a literal template
            if tpl.startswith(("/", "~/")):
                tpls = [tpl]
            elif class_routes:
                tpls = [_combine(r, tpl) for r in literal_routes]
            else:
                tpls = [tpl] if tpl else []
            for t in tpls:
                def token(mt, _action=action):
                    key = mt.group(1).lower()
                    if key == "controller":
                        return ctrl
                    if key == "action":
                        return _action
                    if key == "area" and area:
                        return area
                    return "{}"
                t = _ROUTE_TOKEN.sub(token, t)
                for major in (majors if "apiversion" in t.lower() else [None]):
                    shape = route_shape(t, major)
                    if shape and (verb, shape) not in seen and len(seen) < MAX_SELECTORS:
                        seen.add((verb, shape))
                        out.append((verb, shape, m["name"], auth, m["line"], _optional_tail(t)))
    return out


# --------------------------------------------------------------------------------------------
# minimal APIs

def _chain_root(toks, r, match):
    """Index of the first token of the member-access chain that ends at token r."""
    for _ in range(64):
        if _is(toks, r, ")"):
            o = match[r]
            m = o - 1
            if o < 0 or m < 0 or toks[m][0] != "id":
                return None
            if _is(toks, m - 1, "."):
                r = m - 2
                continue
            return m
        if 0 <= r < len(toks) and toks[r][0] == "id":
            while r >= 2 and _is(toks, r - 1, ".") and toks[r - 2][0] == "id":
                r -= 2
            return r
        return None
    return None


_MAX_ARG = 256                  # tokens; a longer argument is never a literal, and is not copied


def _arg(toks, span):
    s, e = span
    if e - s >= 2 and toks[s][0] == "id" and _is(toks, s + 1, ":"):
        s += 2                                                      # named argument `pattern:`
    return toks[s:e] if e - s <= _MAX_ARG else [("p", "?", 0)]


def _builder_branch(toks, span) -> bool:
    """`app.Map("/x", b => b.Run(...))` is IApplicationBuilder.Map - middleware branching, not an
    endpoint. Its handler is a lambda with one untyped parameter (an endpoint delegate needs typed
    parameters; a RequestDelegate is `async ctx => ...` or `ctx => ctx.Response...`) whose body,
    within its first tokens, calls Run / Use* / Map* on that parameter."""
    s, e = span
    if _is(toks, s, "(") and s + 2 < e and toks[s + 1][0] == "id" and _is(toks, s + 2, ")"):
        name, j = toks[s + 1][1], s + 3
    elif s < e and toks[s][0] == "id" and toks[s][1] != "async":
        name, j = toks[s][1], s + 1
    else:
        return False
    if not _is(toks, j, "=>"):
        return False
    for t in range(j + 1, min(e - 2, j + 65)):
        if toks[t][0] == "id" and toks[t][1] == name and _is(toks, t + 1, ".") \
                and toks[t + 2][0] == "id":
            m = toks[t + 2][1]
            if m == "Run" or m.startswith(("Use", "Map")):
                return True
    return False


def _scan_minimal(toks, match, consts) -> list[dict]:
    eps: list[dict] = []
    groups: dict[str, dict] = {}                 # variable -> group
    at_close: dict[int, tuple] = {}              # index of a call's `)` -> ("group"|"ep", obj)
    n = len(toks)
    for i in range(n):
        k, v, ln = toks[i]
        if k == "p" and v == ";":
            obj = at_close.get(i - 1)
            if obj and obj[0] == "group":
                root = _chain_root(toks, i - 1, match)
                if root is not None and root >= 2 and _is(toks, root - 1, "=") and toks[root - 2][0] == "id":
                    groups[toks[root - 2][1]] = obj[1]
            continue
        if (k != "id" or i < 2 or not _is(toks, i - 1, ".") or not _is(toks, i + 1, "(")
                or match[i + 1] <= i + 1):
            continue
        recv = i - 2
        if toks[recv][0] == "id":
            obj = ("group", groups[toks[recv][1]]) if toks[recv][1] in groups else None
        else:
            obj = at_close.get(recv) if _is(toks, recv, ")") else None
        close = match[i + 1]
        if v in ("RequireAuthorization", "AllowAnonymous"):
            if obj:
                target = obj[1]
                if v == "AllowAnonymous":
                    target["auth"] = "anonymous"
                elif target.get("auth") != "anonymous":
                    target["auth"] = "required"
                at_close[close] = obj
            continue
        args = _split_args(toks, i + 2, close, match)
        if v == "MapGroup":
            parent = obj[1] if obj and obj[0] == "group" else None
            depth = parent["depth"] + 1 if parent else 1
            lit = _literal(_arg(toks, args[0]), consts) if args else None
            prefix = None
            # a prefix never grows past MAX_ROUTE nor nests past _MAX_GROUP_DEPTH: every group
            # keeps its parent alive, so an unbounded prefix would cost memory quadratic in the
            # number of `g = g.MapGroup(...)` lines. Such a group is simply not literal.
            if (lit is not None and depth <= _MAX_GROUP_DEPTH
                    and (parent is None or parent["prefix"] is not None)):
                base = parent["prefix"] if parent else ""
                if len(base) + len(lit) + 1 <= MAX_ROUTE:
                    prefix = _combine(base, lit)
            at_close[close] = ("group", {"prefix": prefix, "auth": None, "parent": parent,
                                         "depth": depth})
            continue
        if v not in _MAP_CALLS:
            if obj:
                at_close[close] = obj                               # WithTags(), WithOpenApi()...
            continue
        if obj and obj[0] != "group":
            continue
        route = _literal(_arg(toks, args[0]), consts) if args else None
        if _MAP_CALLS[v] is None:
            verbs = []
            if len(args) > 1:
                s, e = args[1]
                for t in range(s, min(e, s + _MAX_ARG)):
                    tk, tv, _ = toks[t]
                    if tk == "str" and isinstance(tv, str) and tv.upper() in _VERBS:
                        verbs.append(tv.upper())
                    elif tk == "id" and tv.upper() in _VERBS and _is(toks, t - 1, "."):
                        verbs.append(tv.upper())                    # HttpMethods.Get
            handler_arg = args[2] if len(args) > 2 else None
        else:
            verbs = [_MAP_CALLS[v]]
            handler_arg = args[1] if len(args) > 1 else None
        if route is None or not verbs:
            continue
        if v == "Map" and handler_arg and _builder_branch(toks, handler_arg):
            continue                    # IApplicationBuilder.Map: a pipeline branch, no endpoint
        handler, auth = None, None
        if handler_arg:
            seq = _arg(toks, handler_arg)
            if seq and all(t[0] == "id" or (t[0] == "p" and t[1] == ".") for t in seq) and seq[-1][0] == "id":
                handler = seq[-1][1]
            # attributes on the handler lambda: `[AllowAnonymous] () => ...` (the attribute lists
            # that open the argument only, so nested calls are never rescanned)
            t = handler_arg[0]
            while _is(toks, t, "[") and match[t] > t:
                names = {toks[x][1] for x in range(t + 1, min(match[t], t + 64)) if toks[x][0] == "id"}
                if "AllowAnonymous" in names:
                    auth = "anonymous"
                elif "Authorize" in names and auth is None:
                    auth = "required"
                t = match[t] + 1
        ep = {"verbs": list(dict.fromkeys(verbs)), "route": route, "group": obj[1] if obj else None,
              "auth": auth, "line": ln, "handler": handler}
        eps.append(ep)
        at_close[close] = ("ep", ep)
    return eps


def _minimal_endpoints(ep: dict):
    prefix, auth, g, hops = "", ep["auth"], ep["group"], 0
    if g is not None:
        if g["prefix"] is None:
            return []                                               # a non-literal group prefix
        prefix = g["prefix"]
    while g is not None and hops < 32 and auth is None:
        auth, g, hops = g.get("auth"), g.get("parent"), hops + 1
    tpl = _combine(prefix, ep["route"])
    shape = route_shape(tpl)
    if not shape:
        return []
    opt = _optional_tail(tpl)
    return [(verb, shape, ep["handler"], auth, ep["line"], opt) for verb in ep["verbs"]]


# --------------------------------------------------------------------------------------------
# the pass

def _read(root_res: Path, path: Path) -> str | None:
    """File text, or None when it is missing, too large, unreadable or a link out of the repo."""
    try:
        real = path.resolve(strict=True)
        if not real.is_relative_to(root_res) or not real.is_file():
            return None
        if real.stat().st_size > MAX_BYTES:
            return None
        return real.read_text(encoding="utf-8", errors="replace").lstrip("﻿")
    except (OSError, RuntimeError, ValueError):
        return None


def extract(root: Path, exclude: frozenset[str] = frozenset(),
            include: frozenset[str] = frozenset()) -> dict:
    root = Path(root)
    root_res = root.resolve()
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    seen_edges: set[tuple] = set()

    def add(n: dict) -> str:
        cur = nodes.setdefault(n["id"], {k: v for k, v in n.items() if v is not None})
        for key, val in n.items():
            if val is not None and key not in cur:
                cur[key] = val
        return n["id"]

    def edge(src, dst, relation, confidence="EXTRACTED"):
        if src == dst or (src, dst, relation) in seen_edges:
            return
        seen_edges.add((src, dst, relation))
        edges.append({"source": src, "target": dst, "relation": relation, "confidence": confidence})

    cs_files: list[tuple[Path, str]] = []
    projects: dict[str, list[str]] = {}          # directory rel -> its *.csproj rels
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not skip_dir(d, exclude, include)
                             and (d not in _DOTNET_OUTPUT or d in include))
        d = Path(dirpath)
        drel = "" if d == root else d.relative_to(root).as_posix()
        for fn in sorted(filenames):
            rel = f"{drel}/{fn}" if drel else fn
            low = fn.lower()
            if low.endswith(".cs"):
                cs_files.append((d / fn, rel))
            elif low.endswith(".csproj"):
                projects.setdefault(drel, []).append(rel)

    # Phase 1: const strings, so `[Route(Routes.Orders)]` resolves like a literal.
    raw: dict = {}
    budget = [MAX_CONSTS]
    relevant: list[tuple[Path, str]] = []
    for path, rel in cs_files:
        text = _read(root_res, path)
        if text is None:
            continue
        if any(w in text for w in _RELEVANT):
            relevant.append((path, rel))
        if "const" in text:
            try:
                _collect_consts(_tokenize(text), raw, budget)
            except Exception:
                continue
    consts = _resolve_consts(raw)

    # Phase 2: classes and minimal-API calls per file.
    scanned: list[tuple[str, list, list]] = []
    class_map: dict[str, dict] = {}
    for path, rel in relevant:
        text = _read(root_res, path)
        if text is None:
            continue
        try:
            toks = _tokenize(text)
            match = _pairs(toks)
            classes = _scan_classes(toks, match, consts)
            minimal = _scan_minimal(toks, match, consts) if "Map" in text else []
        except Exception:      # one file the scanner did not foresee never fails the pass
            continue
        for c in classes:
            class_map.setdefault(c["name"], c)
        scanned.append((rel, classes, minimal))

    def owner(rel: str) -> str | None:
        d = posixpath.dirname(rel)
        while True:
            if d in projects:
                return d
            if not d:
                return None
            d = posixpath.dirname(d)

    n_endpoints = [0]
    for rel, classes, minimal in scanned:
        try:
            found = []
            for c in classes:
                if _is_controller(c):
                    found += [(verb, shape, c["name"], meth, auth, line, "controller", opt)
                              for verb, shape, meth, auth, line, opt in _controller_endpoints(c, class_map)]
            for ep in minimal:
                found += [(verb, shape, None, h, auth, line, "minimal", opt)
                          for verb, shape, h, auth, line, opt in _minimal_endpoints(ep)]
        except Exception:
            continue
        found = found[:MAX_ENDPOINTS_PER_FILE]
        if not found:
            continue
        mid = add({"id": f"module:{rel}", "label": rel, "type": "module", "file_type": "csharp",
                   "source_file": rel})
        pdir = owner(rel)
        app_id = None
        if pdir is not None:
            app_id = f"aspnetapp:{pdir or '.'}"
            label = (posixpath.basename(projects[pdir][0]).rsplit(".", 1)[0]
                     if len(projects[pdir]) == 1 else posixpath.basename(pdir) or ".")
            add({"id": app_id, "label": label, "type": "aspnet_app", "file_type": "aspnet",
                 "source_file": projects[pdir][0]})
            for proj in projects[pdir]:
                pnode = _manifest_node(proj)
                if pnode:
                    edge(app_id, add(pnode), "defined_by")
        for verb, shape, ctrl, meth, auth, line, kind, opt in found:
            eid = f"endpoint:{verb} {shape}"
            if eid not in nodes:
                if n_endpoints[0] >= MAX_NODES:
                    break
                n_endpoints[0] += 1
                add({"id": eid, "label": f"{verb} {shape}", "type": "endpoint", "file_type": "aspnet",
                     "source_file": rel, "source_location": f"L{line}", "http_method": verb,
                     "route": shape, "kind": kind, "controller": ctrl, "action": meth, "auth": auth,
                     "optional_segments": opt or None})
            edge(eid, mid, "handled_by")
            if app_id:
                edge(app_id, eid, "exposes")

    return {"nodes": list(nodes.values()), "edges": edges}


# --------------------------------------------------------------------------------------------
# post-merge hook: clients -> endpoints

def _client_segments(path: str) -> list[str]:
    # client paths should already be shapes; drop anything that is not a path, just in case
    if "://" in path:
        rest = path.split("://", 1)[1]
        path = "/" + rest.split("/", 1)[1] if "/" in rest else ""
    path = path.split("?", 1)[0].split("#", 1)[0]
    shape = route_shape(path) or ""
    return [s if s == "{}" else s.casefold() for s in shape.split("/") if s]


def _score(cs: list[str], es: list[str]):
    """(literal matches, parameter-to-parameter matches, exact) or None when a literal differs."""
    lit = both = 0
    exact = True
    for c, e in zip(cs, es):
        if c == e:
            if c == "{}":
                both += 1
            else:
                lit += 1
        elif c == "{}" or e == "{}":
            exact = False
        else:
            return None
    return lit, both, exact


def _match_key(cs: list[str], stripped: list[str], es: list[str], same: bool):
    """(tier, literal matches, parameter matches, same method) of client segments `cs` against
    endpoint segments `es`, or None; `stripped` is cs without its leading `{}` segments."""
    if len(es) == len(cs):
        sc = _score(cs, es)
        if sc:
            return (2 if sc[2] else 1, sc[0], sc[1], same)
    if stripped and len(es) > len(stripped):
        sc = _score(stripped, es[-len(stripped):])
        if sc:
            return (0, sc[0], sc[1], same)
    elif stripped is not cs and len(es) == len(stripped):
        sc = _score(stripped, es)
        if sc:
            return (0, sc[0], sc[1], same)
    return None


def link_calls(nodes) -> list[dict]:
    """INFERRED `calls_api` edges from HTTP client nodes to the endpoints they call.

    `nodes` is every node of the merged graph as a dict with "id" (e.g. `{"id": nid, **attrs}`
    for each NetworkX node). A client is an `appsmithquery:*` or `httprequest:*` node with
    `http_method` (or `method`) and a URL path shape in `route_shape` (or path / path_shape /
    route / url_path);
    an endpoint is a node this pass emitted. Match tiers, best first: `exact` (same shape),
    `pattern` (a `{}` on either side stands for one segment), `suffix` (the client path, its
    leading `{}` base-URL segments dropped, ends the endpoint route - a datasource base URL
    often carries the prefix). Within the best tier the most literal segments win; at least one
    literal segment must match, and a client with more than MAX_LINK_TARGETS best candidates is
    ambiguous and gets no edge. Literal comparison is case-insensitive, as ASP.NET Core routing
    is. Same METHOD, or an endpoint answering ANY. Output is sorted and deterministic."""
    endpoints: dict[str, list] = {}
    clients = []
    for n in nodes:
        if not isinstance(n, dict) or not isinstance(n.get("id"), str):
            continue
        nid = n["id"]
        if nid.startswith("endpoint:") and n.get("type") == "endpoint":
            m, r = n.get("http_method"), n.get("route")
            if isinstance(m, str) and isinstance(r, str):
                segs = _client_segments(r)
                opt = n.get("optional_segments")
                opt = min(opt, len(segs)) if isinstance(opt, int) and not isinstance(opt, bool) and opt > 0 else 0
                endpoints.setdefault(m.upper(), []).append((nid, segs, opt))
        elif nid.startswith(_CLIENT_PREFIXES):
            m = n.get("http_method") or n.get("method")
            p = next((n[k] for k in _CLIENT_PATH_KEYS if isinstance(n.get(k), str)), None)
            if isinstance(m, str) and p is not None:
                clients.append((nid, m.upper(), _client_segments(p)))
    for lst in endpoints.values():
        lst.sort()
    clients.sort()
    out: list[dict] = []
    budget = MAX_LINK_COMPARISONS
    for cid, method, cs in clients:
        if not cs or budget <= 0:
            continue
        stripped = cs
        while stripped and stripped[0] == "{}":
            stripped = stripped[1:]
        best_key, best = None, []
        for same, cands in ((True, endpoints.get(method, [])), (False, endpoints.get("ANY", []))):
            if method == "ANY" and not same:
                continue
            for eid, full, opt in cands:
                key = None
                # the route as written, then without each optional trailing segment
                for drop in range(opt + 1):
                    budget -= 1
                    if budget <= 0:
                        break
                    k = _match_key(cs, stripped, full[:len(full) - drop] if drop else full, same)
                    if k is not None and (key is None or k > key):
                        key = k
                if budget <= 0:
                    break
                if key is None or key[1] < 1:
                    continue
                if best_key is None or key > best_key:
                    best_key, best = key, [eid]
                elif key == best_key:
                    best.append(eid)
        if not best or len(best) > MAX_LINK_TARGETS:
            continue
        tier = {2: "exact", 1: "pattern", 0: "suffix"}[best_key[0]]
        for eid in sorted(set(best)):
            out.append({"source": cid, "target": eid, "relation": "calls_api",
                        "confidence": "INFERRED", "match": tier})
    return out
