"""HTTP request-file extractor - which routes a repository's REST-client files call, and how.

Request files (`*.http`, `*.rest`) are what the VS Code REST Client, the JetBrains HTTP Client and
Visual Studio run by hand against an API. No other pass reads them, yet they say which routes a
service answers, which requests need credentials and which request feeds another. This
repo-level pass reads (stdlib only, no network, no template evaluation):

  blocks          requests separated by `###` lines (any text after `###` is a comment)
  `# @name x`     a request's name (`// @name x` too); `# @prompt v` names a prompted variable
  request line    `METHOD URL [HTTP/x.y]`, or a bare `http(s)://...` / `{{base}}...` URL (GET);
                  query-continuation lines after it (`?a=1`, `&b=2`) are dropped
  headers         `Name: value` lines up to the first blank line - the NAME only
  body            the rest of the block, scanned only for `{{variable}}` names, `< ./file` (a
                  body read from a file) and `> ./handler.js` (a response-handler script)
  `@name = v`     file variables - the NAME only. A variable that opens a request URL and itself
                  holds a URL or path (`@base = {{host}}/api`) is substituted, in memory, as one
                  `{}` hole per path segment it contributes, never its text, so the route shape
                  keeps the base path's length (`GET {{base}}/items` -> `/{}/items`)
  `< ./pre.js`    before the request line: a pre-request script (JetBrains)
  `import ./x.http`, `run ./x.http`, `run #Name`   JetBrains imports and runs

Emitted nodes:
  httpfile:<rel>             a request file - attrs: variables (file-variable names),
                             request_count
  httprequest:<rel>#<key>    a request; key = its `@name`, else its 1-based position in the
                             file (a numeric or repeated name falls back to the position).
                             attrs: method, route_shape, name, base_variable (the variable the
                             URL opens with), headers (names), uses_auth, variables (names
                             used, `$guid`-style system variables by their first word),
                             prompts (names), has_body
  module:<rel> / file:<rel>  a local body file or handler script, by docker.file_node: the id
                             the python / tree-sitter / manifests passes give that file
Emitted edges:
  has_request     httpfile -> httprequest
  uses_response   httprequest -> the named request of the same file whose response it reads
                  (`{{login.response.body.$.token}}`)
  body_from       httprequest -> local file (`< ./payload.json`)
  runs            httprequest -> handler script (phase: pre_request | response); httpfile ->
                  httpfile or httprequest (`run`, a `#Name` looked up in the file itself, then
                  in the files it imports)
  imports         httpfile -> httpfile

route_shape is the URL path under the shape rule of the Appsmith pass (`extract_appsmith`
`_path_only`, then its canonical `route_shape` form), so the two passes join on it: the query
string and fragment are cut first (whatever they hold), then scheme, userinfo, host and port
(the first segment of a scheme-less URL is its host), a leading `{{base}}` binding (the host or
base URL, normally defined per environment) and matrix parameters are dropped; a binding inside
the path, and every segment a substituted file variable contributes, becomes `{}`; every
segment that is not a plain word (letters, `-`, `_`, at most 24) or `v<n>` becomes `{}`; one
leading `/`, no trailing `/`. `{{host}}/api/items/{{id}}?expand=1` -> `/api/items/{}`.

Deliberately absent - a hard rule, not an omission: every VALUE. No file-variable values, no
header values (`uses_auth` says an Authorization / API-key / token / cookie-style header is
present, never what it holds, nor its scheme), no request bodies, no query strings or parameter
names, no hosts, ports or userinfo, no comments or `###` titles, no script code (inline `{% %}`
blocks are skipped whole), no `run` arguments, no curl lines. Environment files
(`http-client*.env.json`, `.vscode/settings.json`) are never read. A file over MAX_BYTES,
outside the repository through a link, or unreadable is skipped; a file that still trips the
pass is dropped whole, never the build. Local paths are looked up at most MAX_PROBES times per
file (each distinct path once, MAX_LOCAL per request and list), so a file of `< ./x` lines
cannot turn into unbounded filesystem work.
"""
from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path

from .extract_ansible import skip_dir
from .extract_appsmith import _mustache_spans, _path_only, route_shape
from .extract_docker import _join, _kind, file_node

MAX_BYTES = 2 * 1024 * 1024    # a larger request file is generated or a dump, not authored
MAX_REQUESTS = 2000            # requests graphed per file
MAX_HEADERS = 100              # header names kept per request
MAX_NAMES = 500                # variable / prompt names kept per file or request
MAX_EDGES = 4000               # edges per file
MAX_LOCAL = 32                 # body files / handlers / pre-request scripts kept per request
MAX_PROBES = 1000              # filesystem lookups (local files, imports, runs) per file
_MAX_URL = 4096                # longest request URL examined (after base substitution)
_MAX_SUBST = 8                 # nested `{{base}}` substitutions followed at the start of a URL
_MAX_BINDING = 256             # a `{{ }}` longer than this is not a variable reference
_SUFFIXES = (".http", ".rest")

_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE",
                      "CONNECT", "GRAPHQL", "WEBSOCKET", "GRPC"})
_HTTP_VERSION = re.compile(r"HTTP/\d(?:\.\d)?", re.I)
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,15}://")
_HEADER = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}")
_VAR = re.compile(r"[A-Za-z_][\w.-]{0,127}")
_SYSVAR = re.compile(r"\$[A-Za-z][\w.]{0,63}")
_REQ_NAME = re.compile(r"\w[\w.-]{0,63}")
_AUTH_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "x-api-key",
                           "api-key", "apikey", "x-apikey", "ocp-apim-subscription-key",
                           "x-functions-key", "private-token", "x-vault-token"})
_AUTH_WORDS = re.compile(r"api[-_]?key|auth|token|secret|session|signature|credential|"
                         r"password|access[-_]?key|subscription[-_]?key")


def is_request_file(name: str) -> bool:
    return name.lower().endswith(_SUFFIXES)


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


def _is_auth_header(name: str) -> bool:
    low = name.lower()
    return low in _AUTH_HEADERS or bool(_AUTH_WORDS.search(low))


def _urlish(value: str) -> bool:
    """Whether a file variable's value is a URL or path worth substituting into a route. Anything
    else (a bare host, a token, a number) is never placed in a route."""
    v = value.strip()
    return v.startswith(("/", "{{")) or bool(_SCHEME.match(v))


def _looks_like_url(token: str) -> bool:
    t = token.lower()
    return t.startswith(("/", "{{")) or bool(_SCHEME.match(t)) or t.startswith("localhost")


def _request_line(line: str) -> tuple[str, str] | None:
    """(METHOD, URL text) of a request line, or None when the line is not one."""
    parts = line.split()
    if not parts:
        return None
    head = parts[0]
    if head.upper() in _METHODS and len(parts) >= 2 and (
            head.isupper() or _looks_like_url(parts[1])):
        method, rest = head.upper(), parts[1:]
    elif _SCHEME.match(head) or head.startswith("{{"):
        method, rest = "GET", parts
    else:
        return None
    if len(rest) > 1 and _HTTP_VERSION.fullmatch(rest[-1]):
        rest = rest[:-1]
    return method, " ".join(rest)


def _mask_bindings(s: str) -> str:
    """Every `{{ ... }}` -> \\x00, so a `/`, `?` or `#` inside one cannot split the URL."""
    s = s.replace("\x00", "")
    spans = _mustache_spans(s)
    if not spans:
        return s
    parts, pos = [], 0
    for a, b in spans:
        parts += [s[pos:a], "\x00"]
        pos = b
    parts.append(s[pos:])
    return "".join(parts)


def _cut_query(s: str) -> str:
    """Drop the query string and fragment. First, before any host logic: a query may hold `/` or
    `://` itself, and RFC 3986 allows neither `?` nor `#` unescaped in userinfo or host."""
    return s.split("?", 1)[0].split("#", 1)[0]


def _path_part(s: str) -> str:
    """The path of a masked URL: scheme, userinfo, host and port (or a leading binding standing
    for them) dropped. Without a scheme the first segment is the host, as REST Client reads a
    bare `host/x`."""
    if "://" in s:
        s = s.split("://", 1)[1]
    elif s.startswith("//"):
        s = s[2:]
    elif s.startswith("/"):
        return s
    _head, sep, rest = s.partition("/")
    return "/" + rest.lstrip("/") if sep else ""      # `h//x`: never read `//x` as a host


def _masked_value(value: str) -> str:
    """A file variable's URL value as it enters a route: query, fragment, scheme and host gone,
    and every path segment a `{{}}` hole. The value's text never reaches the shape - only how
    many segments it contributes (`https://h/api/v2` -> `/{{}}/{{}}`). A leading `{{x}}` stays,
    for the next substitution round."""
    v = value.strip()
    head = ""
    if v.startswith("{{"):
        end = v.find("}}", 2)
        if end < 0:
            return ""
        head, v = v[:end + 2], v[end + 2:]
    v = _cut_query(_mask_bindings(v))
    if head:
        cut = v.find("/")                     # `{{host}}:8080/api` -> `/api`
        path = v[cut:] if cut >= 0 else ""
    else:
        path = _path_part(v)
    return head + "".join("/{{}}" for seg in path.split("/") if seg.strip())


def _route(url: str, file_vars: dict[str, str]) -> tuple[str | None, str | None]:
    """(route_shape, base_variable) of a request URL. Values never leave this function: a
    leading file variable holding a URL is substituted only as holes, one per path segment."""
    url = url.strip()[:_MAX_URL]
    base_var = None
    for _ in range(_MAX_SUBST):
        if not url.startswith("{{"):
            break
        end = url.find("}}", 2)
        if end < 0:
            break
        name = url[2:end].strip()
        if base_var is None and _VAR.fullmatch(name):
            base_var = name
        val = file_vars.get(name)
        if val is None or not _urlish(val):
            break
        url = (_masked_value(val) + url[end + 2:])[:_MAX_URL]
    p = _cut_query(_mask_bindings(url))
    if p.startswith("\x00"):
        # a leading binding is the host or base URL: drop up to the first `/`
        _head, sep, rest = p.partition("/")
        p = "/" + rest.lstrip("/") if sep else ""
    else:
        p = _path_part(p)
    p = p.replace("\x00", "{{}}")
    shape = route_shape(_path_only(p)) if p else None
    return shape or "/", base_var


def _uses(s: str, req: dict):
    """Variable names and request references in the `{{ }}` bindings of s (never the rest)."""
    for a, b in _mustache_spans(s):
        inner = s[a + 2:b - 2].strip()
        if not inner or len(inner) > _MAX_BINDING:
            continue
        if inner.startswith("$"):
            m = _SYSVAR.match(inner)
            if m and len(req["vars"]) < MAX_NAMES:
                req["vars"].add(m.group(0))
            continue
        segs = inner.split(".")
        for i in range(1, len(segs) - 1):
            if segs[i] in ("response", "request"):
                ref = ".".join(segs[:i])
                if _REQ_NAME.fullmatch(ref):
                    req["refs"].add(ref)
                break
        else:
            if _VAR.fullmatch(inner) and len(req["vars"]) < MAX_NAMES:
                req["vars"].add(inner)


def _directive(comment: str, word: str) -> str | None:
    """`@name x` / `@name = x` -> x (its first word), for a comment body."""
    if not comment.startswith(word):
        return None
    rest = comment[len(word):]
    if rest and not (rest[0].isspace() or rest[0] in "=:"):
        return None
    rest = rest.lstrip(" \t=:")
    tok = rest.split()[0] if rest.split() else ""
    return tok or None


def _parse(text: str) -> dict:
    """One pass over the lines; linear in the file size."""
    file_vars: dict[str, str] = {}
    imports: list[str] = []
    runs: list[str] = []
    requests: list[dict] = []
    state = "pre"           # pre | query | headers | body | skip
    cur: dict | None = None
    name = None
    prompts: list[str] = []
    pre_scripts: list[str] = []
    in_script = False
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if in_script:
            if "%}" in line:
                in_script = False
            continue
        if line.startswith("###"):
            state, cur, name, prompts, pre_scripts = "pre", None, None, [], []
            continue
        if state == "skip":
            continue
        if state == "pre":
            if not line:
                continue
            if line.startswith(("#", "//")):
                c = line.lstrip("#/").strip()
                n = _directive(c, "@name")
                if n is not None:
                    name = n if _REQ_NAME.fullmatch(n) else None
                p = _directive(c, "@prompt")
                if p is not None and _VAR.fullmatch(p) and len(prompts) < MAX_NAMES:
                    prompts.append(p)
                continue
            if line.startswith("@"):
                eq = line.find("=")
                if eq > 0:
                    vname = line[1:eq].strip()
                    if _VAR.fullmatch(vname) and (vname in file_vars or len(file_vars) < MAX_NAMES):
                        file_vars[vname] = line[eq + 1:].strip()
                continue
            if line.startswith("<"):
                rest = line[1:].strip()
                if rest.startswith("{%"):
                    in_script = "%}" not in rest[2:]
                elif rest and len(pre_scripts) < MAX_LOCAL:
                    pre_scripts.append(rest)
                continue
            low = line.lower()
            if low.startswith("import") and low[6:7].isspace():
                if len(imports) < MAX_NAMES:
                    imports.append(line[6:].strip())
                continue
            if low.startswith("run") and low[3:4].isspace():
                if len(runs) < MAX_NAMES:
                    runs.append(line[3:].strip())
                continue
            req = _request_line(line)
            if req is None:
                continue                        # curl lines and anything unrecognised
            if len(requests) >= MAX_REQUESTS:
                state = "skip"
                continue
            cur = {"line": i, "method": req[0], "url": req[1], "name": name,
                   "prompts": prompts, "pre": pre_scripts, "headers": [], "auth": False,
                   "vars": set(), "refs": set(), "body": False, "body_files": [],
                   "handlers": []}
            requests.append(cur)
            _uses(req[1], cur)
            state = "query"
            continue
        if cur is None:
            continue
        if state in ("query", "headers") and line[:1] in ("<", ">"):
            state = "body"                      # a body file or handler with no blank line
        if state in ("query", "headers"):
            if not line:
                state = "body"
                continue
            if state == "query" and line[0] in "?&":
                _uses(line, cur)
                continue
            state = "headers"
            if line.startswith(("#", "//")):
                continue
            colon = line.find(":")
            hname = line[:colon].strip() if colon > 0 else ""
            if _HEADER.fullmatch(hname):
                if not any(h.lower() == hname.lower() for h in cur["headers"]) \
                        and len(cur["headers"]) < MAX_HEADERS:
                    cur["headers"].append(hname)
                if _is_auth_header(hname):
                    cur["auth"] = True
                _uses(line[colon + 1:], cur)
            continue
        # body
        if not line:
            continue
        if line.startswith(">"):
            if line.startswith(">>"):
                continue                        # response written to a file: output, not input
            rest = line[1:].strip()
            if rest.startswith("{%"):
                in_script = "%}" not in rest[2:]
            elif rest and len(cur["handlers"]) < MAX_LOCAL:
                cur["handlers"].append(rest)
            continue
        if line.startswith("<>"):
            continue                            # JetBrains reference to an earlier response
        if line.startswith("<"):
            rest = line[1:].strip()
            if rest.startswith("{%"):
                in_script = "%}" not in rest[2:]
            elif rest:
                if len(cur["body_files"]) < MAX_LOCAL:
                    cur["body_files"].append(rest)
                cur["body"] = True
            continue
        if line.startswith(("#", "//")) and not cur["body"]:
            continue                            # a comment between headers and handler
        cur["body"] = True
        _uses(line, cur)

    used: set[str] = set()
    names: dict[str, str] = {}
    for n, r in enumerate(requests, 1):
        nm = r["name"]
        key = nm if nm and not nm.isdigit() and nm not in used else str(n)
        used.add(key)
        r["key"] = key
        if nm:
            names.setdefault(nm, key)
    return {"vars": file_vars, "imports": imports, "runs": runs, "requests": requests,
            "names": names}


def _http_file_node(rel: str, **attrs) -> dict:
    return {"id": f"httpfile:{rel}", "label": posixpath.basename(rel), "type": "http_file",
            "file_type": "http", "source_file": rel,
            **{k: v for k, v in attrs.items() if v is not None}}


def _clean_path(p: str) -> str:
    p = p.strip()
    if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
        p = p[1:-1].strip()
    return p


def extract(root: Path, exclude: frozenset[str] = frozenset(),
            include: frozenset[str] = frozenset()) -> dict:
    root = Path(root)
    root_res = root.resolve()
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    seen: set[tuple] = set()

    files: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not skip_dir(d, exclude, include))
        d = Path(dirpath)
        drel = "" if d == root else d.relative_to(root).as_posix()
        for fn in sorted(filenames):
            if is_request_file(fn):
                files.append((d / fn, f"{drel}/{fn}" if drel else fn))

    parsed: dict[str, dict] = {}
    for path, rel in files:
        text = _read(root_res, path)
        if text is None:
            continue
        try:
            parsed[rel] = _parse(text)
        except Exception:
            continue                            # a shape the parser did not foresee: skip it

    probes = [MAX_PROBES]           # filesystem lookups left for the file being linked
    looked: dict[str, str | None] = {}

    def kind(t: str) -> str | None:
        """_kind, at most once per path and MAX_PROBES times per file."""
        if t in looked:
            return looked[t]
        if probes[0] <= 0:
            return None
        probes[0] -= 1
        looked[t] = _kind(root_res, t)
        return looked[t]

    def resolve_http(base: str, target: str) -> str | None:
        """httpfile id of an imported / run request file, adding its node when the file exists
        but was not parsed (excluded directory, oversized)."""
        t = _join(base, _clean_path(target))
        if t is None or not is_request_file(t):
            return None
        if t not in parsed:
            if kind(t) != "file":
                return None
            n = _http_file_node(t)
            nodes.setdefault(n["id"], n)
        return f"httpfile:{t}"

    for rel, doc in parsed.items():
        n_nodes, n_edges = len(nodes), len(edges)
        budget = [MAX_EDGES]
        probes[0] = MAX_PROBES
        looked.clear()

        def edge(src, dst, relation, confidence="EXTRACTED", **attrs):
            attrs = {k: v for k, v in attrs.items() if v is not None}
            key = (src, dst, relation, tuple(sorted(attrs.items())))
            if src == dst or key in seen or budget[0] <= 0:
                return
            budget[0] -= 1
            seen.add(key)
            edges.append({"source": src, "target": dst, "relation": relation,
                          "confidence": confidence, **attrs})

        def local(base: str, p: str) -> str | None:
            if budget[0] <= 0:
                return None                     # no edge could be written: do not look
            t = _join(base, _clean_path(p))
            if t is None or kind(t) != "file":
                return None
            n = file_node(t)
            nodes.setdefault(n["id"], n)
            return n["id"]

        try:
            base = posixpath.dirname(rel)
            fid = f"httpfile:{rel}"
            n = _http_file_node(rel, variables=sorted(doc["vars"]) or None,
                                request_count=len(doc["requests"]))
            nodes[fid] = n
            for r in doc["requests"]:
                rid = f"httprequest:{rel}#{r['key']}"
                shape, base_var = _route(r["url"], doc["vars"])
                nodes[rid] = {
                    "id": rid, "label": r["name"] or f"{r['method']} {shape}",
                    "type": "http_request", "file_type": "http", "source_file": rel,
                    "source_location": f"L{r['line']}", "method": r["method"],
                    "route_shape": shape,
                    **{k: v for k, v in (
                        ("name", r["name"]), ("base_variable", base_var),
                        ("headers", r["headers"] or None),
                        ("uses_auth", True if r["auth"] else None),
                        ("variables", sorted(r["vars"]) or None),
                        ("prompts", sorted(set(r["prompts"])) or None),
                        ("has_body", True if r["body"] else None)) if v is not None}}
                edge(fid, rid, "has_request")
            for r in doc["requests"]:
                rid = f"httprequest:{rel}#{r['key']}"
                for ref in sorted(r["refs"]):
                    key = doc["names"].get(ref)
                    if key:
                        edge(rid, f"httprequest:{rel}#{key}", "uses_response")
                for p in r["pre"]:
                    t = local(base, p)
                    if t:
                        edge(rid, t, "runs", phase="pre_request")
                for p in r["handlers"]:
                    t = local(base, p)
                    if t:
                        edge(rid, t, "runs", phase="response")
                for p in r["body_files"]:
                    t = local(base, p)
                    if t:
                        edge(rid, t, "body_from")
            imported: list[str] = []
            for p in doc["imports"]:
                t = resolve_http(base, p)
                if t:
                    edge(fid, t, "imports")
                    imported.append(t[len("httpfile:"):])
            for p in doc["runs"]:
                target = p.split("(", 1)[0].strip()     # `(@var=value)` arguments: never read
                if target.startswith("#"):
                    want = target[1:].strip()
                    for src in [rel] + imported:
                        key = parsed.get(src, {}).get("names", {}).get(want)
                        if key:
                            edge(fid, f"httprequest:{src}#{key}", "runs")
                            break
                elif target:
                    t = resolve_http(base, target)
                    if t:
                        edge(fid, t, "runs")
        except Exception:
            # drop what the file had added, so a half-read file does not reach the graph
            for nid in list(nodes)[n_nodes:]:
                del nodes[nid]
            for e in edges[n_edges:]:
                seen.discard((e["source"], e["target"], e["relation"],
                              tuple(sorted((k, v) for k, v in e.items()
                                           if k not in ("source", "target", "relation",
                                                        "confidence")))))
            del edges[n_edges:]

    return {"nodes": list(nodes.values()), "edges": edges}
