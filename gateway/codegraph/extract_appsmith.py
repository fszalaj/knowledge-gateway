"""Appsmith extractor - the wiring of a low-code app that no generic pass can see.

An Appsmith git-sync export is a tree of JSON files plus a few `.txt` and `.js` bodies. A generic
JSON pass would see thousands of keys; what matters is which button runs which query, which table
shows which query's data, and which datasource a query talks to. Those links live inside `{{ }}`
bindings, so this pass reads the shapes Appsmith writes and resolves names per page:

  <app>/metadata.json                         export marker (artifactJsonType / fileFormatVersion)
  <app>/application.json                      app name (optional) and the page list
  <app>/datasources/<name>.json               datasource name and plugin
  <app>/pages/<page>/<page>.json              page name; older exports also hold the widget DSL here
  <app>/pages/<page>/widgets/**.json          one widget per file (format v5), nesting via parentId
  <app>/pages/<page>/queries/<q>/metadata.json  query: plugin, HTTP method, path, datasource
  <app>/pages/<page>/queries/<q>/<q>.txt      query body (scanned for bindings, never copied)
  <app>/pages/<page>/jsobjects/<j>/<j>.js     JS object body (scanned for names, never copied)
  (older flat forms `queries/<q>.json` + `<q>.txt` and `jsobjects/<j>.js` are read too)

An app root is a directory holding a metadata.json that carries "artifactJsonType" or
"fileFormatVersion", next to application.json and a pages/ directory. It may sit anywhere below
the repository root; the walk does not descend into an app once found.

Emitted nodes:
  `appsmith:<app>`                         the application
  `appsmithpage:<app>/<page>`              a page
  `appsmithquery:<app>/<page>/<query>`     a query/API - attrs: plugin, plugin_type, http_method,
                                           path (URL path shape only, see below), on_page_load
  `appsmithdatasource:<app>/<name>`        a datasource - attrs: plugin only
  `appsmithwidget:<app>/<page>/<widget>`   a widget - attrs: widget_type
  `appsmithjs:<app>/<page>/<jsobject>`     a JS object
Emitted edges:
  contains          app -> page / datasource, page -> query / JS object / top-level widget,
                    widget -> nested widget (canvas containers collapsed)
  uses_datasource   query -> datasource
  triggers          widget event binding (or JS object code) that calls `<Query>.run(...)` or
                    `<JSObject>.<fn>(...)`
  reads             binding that reads `<Query>.data`
  references        any other whole-identifier mention of a real name on the same page, from a
                    widget, query or JS object (confidence INFERRED: it may sit in a string)
  navigates_to      `navigateTo('<Page>')` naming a real page of the same app
  implemented_by    JS object -> `module:<rel>` of its .js file. The pass writes that node itself,
                    in the shape the tree-sitter pass gives it, so the target is typed without
                    tree-sitter and the two merge cleanly when both run.

Deliberately absent - this is a hard rule, not an omission: no values. Headers, bodies, query
parameters, form data, full URLs, hosts, query strings, datasource configuration (URLs,
credentials, auth), widget defaults/labels/text and JS code are never copied; only names, types
and the *shape* of the URL path. Scheme, userinfo, host, query string, fragment and matrix
parameters are stripped; a `{{ }}` binding becomes `{}`; and every literal segment that is not a
plain word (letters, `-` or `_`, at most 24 characters, or a `v<n>` version) also becomes `{}`,
because a webhook or bot token, or an API key, is often carried in the path itself. Canvas
widgets (layout plumbing), themes and gitSyncIds are not graphed. Names are resolved per page,
as Appsmith scopes them; nothing is evaluated.

Structure only: stdlib JSON parsing, no network, no template evaluation, no subprocess. A file
that is malformed (broken JSON, nesting too deep, or valid JSON of the wrong shape), oversized
or unreadable is skipped; an app whose export still trips the pass is dropped whole, never the
build.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .extract_ansible import skip_dir
from .treesitter import EXT_LANG

_MAX_BYTES = 2 * 1024 * 1024        # per-file read cap; larger files are skipped
_MAX_WIDGETS_PER_FILE = 5000        # a widget file/page DSL holding more than this is truncated
_MAX_NODES_PER_APP = 20000          # hard ceiling per app, whatever the export claims
_MAX_STRINGS = 200_000              # strings visited under one widget or query
_MAX_PATH_LEN = 512
_CANVAS = "CANVAS_WIDGET"
_EMBEDDED_DS = {"DEFAULT_REST_DATASOURCE", ""}

_IDENT = re.compile(r"(?<![\w$.])[A-Za-z_$][\w$]*")
# A path segment kept verbatim: a plain word or an API version. Anything else (digits mixed in,
# a long run, `:` `@` `=` `%` `+` ...) may be a token and is reduced to `{}`.
_PLAIN_SEGMENT = re.compile(r"[A-Za-z][A-Za-z_-]{0,23}|v\d{1,2}")
_NAVIGATE = re.compile(r"""navigateTo\s*\(\s*(['"])([^'"\n]{1,200})\1""")
_EVENT_KEY = re.compile(r"^on[A-Z]")
_SKIP_WIDGET_KEYS = {"children", "dynamicBindingPathList", "dynamicTriggerPathList",
                     "dynamicPropertyPathList"}


class _Reader:
    """Contained, capped reads. A file outside root (via symlink), too large, or unreadable is
    simply absent - it never fails the build."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def ok(self, p: Path) -> bool:
        try:
            r = p.resolve()
            r.relative_to(self.root)
        except (OSError, ValueError):
            return False
        return True

    def text(self, p: Path) -> str | None:
        try:
            if not self.ok(p) or not p.is_file() or p.stat().st_size > _MAX_BYTES:
                return None
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def json(self, p: Path):
        t = self.text(p)
        if t is None:
            return None
        try:
            return json.loads(t)
        except (ValueError, RecursionError):    # broken, or nested deeper than the parser allows
            return None

    def is_dir(self, p: Path) -> bool:
        try:
            return p.is_dir() and not p.is_symlink() and self.ok(p)
        except OSError:
            return False

    def is_file(self, p: Path) -> bool:
        try:
            return p.is_file() and self.ok(p)
        except OSError:
            return False

    def subdirs(self, d: Path, exclude, include) -> list[Path]:
        try:
            names = sorted(e.name for e in os.scandir(d) if e.is_dir(follow_symlinks=False))
        except OSError:
            return []
        return [d / n for n in names
                if (n in include or not (n.startswith(".") or n in exclude))]

    def files(self, d: Path, suffix: str) -> list[Path]:
        try:
            names = sorted(e.name for e in os.scandir(d)
                           if e.is_file(follow_symlinks=False) and e.name.endswith(suffix))
        except OSError:
            return []
        return [d / n for n in names]

    def tree(self, d: Path, suffix: str, exclude, include) -> list[Path]:
        """Every `suffix` file below d, sorted, never through a symlinked directory."""
        out: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(d):
            base = Path(dirpath)
            dirnames[:] = sorted(n for n in dirnames
                                 if not (base / n).is_symlink()
                                 and (n in include or not (n.startswith(".") or n in exclude)))
            out.extend(base / f for f in sorted(filenames) if f.endswith(suffix))
        return out


def _is_app_root(rd: _Reader, d: Path) -> bool:
    if not (rd.is_file(d / "application.json") and rd.is_dir(d / "pages")):
        return False
    meta = rd.json(d / "metadata.json")
    return isinstance(meta, dict) and ("artifactJsonType" in meta or "fileFormatVersion" in meta)


def _app_roots(root: Path, rd: _Reader, exclude, include) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, _ in os.walk(root):
        d = Path(dirpath)
        if _is_app_root(rd, d):
            found.append(d)
            dirnames[:] = []        # an app's own tree is read by the app pass, not re-detected
            continue
        dirnames[:] = sorted(n for n in dirnames
                             if not skip_dir(n, exclude, include) and not (d / n).is_symlink())
    return sorted(found)


def _str(v) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _mustache_spans(s: str) -> list[tuple[int, int]]:
    """(start, end) of every `{{ ... }}` in s, the first `}}` closing each. A linear scan: an
    unclosed `{{` ends the search for good, where a lazy `.*?` regex would rescan the rest of
    the string from every later `{{` (quadratic on a file of unclosed braces)."""
    out, pos = [], 0
    while True:
        a = s.find("{{", pos)
        if a < 0:
            break
        b = s.find("}}", a + 2)
        if b < 0:
            break
        out.append((a, b + 2))
        pos = b + 2
    return out


def _path_only(p) -> str | None:
    """The shape of the URL path, nothing else: scheme/userinfo/host, query string, fragment and
    matrix parameters are dropped, bindings become {}, and so does every segment that is not a
    plain word (see _PLAIN_SEGMENT) - a token in the path must never reach the graph."""
    if not isinstance(p, str):
        return None
    p = p.strip()
    spans = _mustache_spans(p)
    if spans:
        parts, pos = [], 0
        for a, b in spans:
            parts += [p[pos:a], "\x00"]
            pos = b
        parts.append(p[pos:])
        p = "".join(parts)
    p = p.replace("{{", "").replace("}}", "")
    if "://" in p:
        rest = p.split("://", 1)[1]
        p = "/" + rest.split("/", 1)[1] if "/" in rest else ""
    elif p.startswith("//"):
        rest = p[2:]
        p = "/" + rest.split("/", 1)[1] if "/" in rest else ""
    elif not p.startswith("/"):
        # scheme-less `user:pw@host/x` or `host.example.com/x`: the first segment is a host
        head, sep, rest = p.partition("/")
        if any(c in head for c in "@:."):
            p = "/" + rest if sep else ""
    for sep in ("?", "#", ";"):
        p = p.split(sep, 1)[0]
    segs = []
    for seg in p.strip().split("/"):
        seg = seg.strip()
        if seg == "" or seg == "\x00" or _PLAIN_SEGMENT.fullmatch(seg):
            segs.append(seg)
        else:
            segs.append("{}")
    p = "/".join(segs).replace("\x00", "{}")
    return p[:_MAX_PATH_LEN] or None


def _action(meta: dict) -> dict:
    """The action/collection body; the export keeps pluginId/pluginType one level up."""
    outer = {k: meta[k] for k in ("pluginId", "pluginType") if k in meta}
    for k in ("unpublishedAction", "publishedAction", "unpublishedCollection", "publishedCollection"):
        v = meta.get(k)
        if isinstance(v, dict):
            return {**outer, **v}
    return meta


def _strings(obj, path: str = ""):
    """(dotted path, string) for every string under obj, in document order. Iterative and
    bounded, so JSON nested as deep as the parser allows cannot overflow the stack."""
    stack = [(path, obj)]
    visited = 0
    while stack and visited < _MAX_STRINGS:
        visited += 1
        p, o = stack.pop()
        if isinstance(o, str):
            yield p, o
        elif isinstance(o, dict):
            stack.extend(reversed([(f"{p}.{k}" if p else k, v) for k, v in o.items()
                                   if isinstance(k, str)]))
        elif isinstance(o, list):
            stack.extend(reversed([(f"{p}[{i}]", v) for i, v in enumerate(o)]))


def _bindings(s: str) -> str:
    """Only the `{{ ... }}` part of a value is code; the rest is a literal and never scanned."""
    if "{{" not in s:
        return ""
    t = s.strip()
    if t.startswith("{{") and t.endswith("}}"):
        return t        # one whole binding; JS inside may itself contain `}}`
    return "\n".join(s[a:b] for a, b in _mustache_spans(s))


def _widgets_in(obj, out: list, limit: int):
    """Flatten a widget dict and its embedded `children` (older exports nest the whole DSL)."""
    stack = [obj]
    while stack and len(out) < limit:
        w = stack.pop()
        if not isinstance(w, dict):
            continue
        if _str(w.get("widgetName")):
            out.append(w)
        kids = w.get("children")
        if isinstance(kids, list):
            stack.extend(reversed(kids))


def extract(root: Path, exclude: frozenset[str] = frozenset(),
            include: frozenset[str] = frozenset()) -> dict:
    root = Path(root)
    rd = _Reader(root)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    seen_edges: set[tuple] = set()
    used_apps: set[str] = set()

    def rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    def edge(src, dst, relation, confidence="EXTRACTED"):
        if src == dst or (src, dst, relation) in seen_edges:
            return
        seen_edges.add((src, dst, relation))
        edges.append({"source": src, "target": dst, "relation": relation, "confidence": confidence})

    def one_app(app_dir: Path):
        budget = [_MAX_NODES_PER_APP]

        def add(nid, **attrs):
            if nid not in nodes:
                if budget[0] <= 0:
                    return None
                budget[0] -= 1
                nodes[nid] = {"id": nid, **{k: v for k, v in attrs.items() if v is not None}}
            return nid

        appj = rd.json(app_dir / "application.json")
        appj = appj if isinstance(appj, dict) else {}
        app = _str(appj.get("name")) or (app_dir.name if app_dir != root else root.resolve().name)
        app = app.strip()
        if app in used_apps:                      # two apps, one name: the path disambiguates
            app = rel(app_dir)
        used_apps.add(app)
        app_rel = rel(app_dir) if app_dir != root else "."
        app_id = add(f"appsmith:{app}", label=app, type="appsmith_app", file_type="appsmith",
                     source_file=(f"{app_rel}/application.json" if app_rel != "." else "application.json"))
        # Valid JSON of the wrong shape is as common as broken JSON: check types before use.
        app_pages = appj.get("pages")
        default_pages = {p["id"] for p in (app_pages if isinstance(app_pages, list) else [])
                         if isinstance(p, dict) and p.get("isDefault") is True
                         and isinstance(p.get("id"), str)}

        # Datasources: name and plugin only. datasourceConfiguration (URL, auth) is never read out.
        ds_ids: dict[str, str] = {}
        ds_dir = app_dir / "datasources"
        if rd.is_dir(ds_dir):
            for f in rd.files(ds_dir, ".json"):
                dj = rd.json(f)
                if not isinstance(dj, dict):
                    continue
                name = _str(dj.get("name")) or f.stem
                did = add(f"appsmithdatasource:{app}/{name}", label=name, type="appsmith_datasource",
                          file_type="appsmith", source_file=rel(f), plugin=_str(dj.get("pluginId")))
                if did:
                    ds_ids[name] = did
                    edge(app_id, did, "contains")

        pages_dir = app_dir / "pages"
        page_dirs = rd.subdirs(pages_dir, exclude, include)

        # Page names first: navigateTo() can point at a page that is graphed later.
        page_meta: list[tuple[Path, str, dict]] = []
        for pd in page_dirs:
            pj = rd.json(pd / f"{pd.name}.json")
            if not isinstance(pj, dict):
                cands = [f for f in rd.files(pd, ".json")]
                pj = rd.json(cands[0]) if len(cands) == 1 else None
            pj = pj if isinstance(pj, dict) else {}
            page = pj.get("unpublishedPage") or pj.get("publishedPage") or pj
            pname = _str(page.get("name")) if isinstance(page, dict) else None
            page_meta.append((pd, (pname or pd.name).strip(), page if isinstance(page, dict) else {}))
        page_ids = {pname: f"appsmithpage:{app}/{pname}" for _, pname, _ in page_meta}
        for pd, pname, _page in page_meta:
            pj_path = pd / f"{pd.name}.json"
            pid = add(page_ids[pname], label=pname, type="appsmith_page", file_type="appsmith",
                      source_file=rel(pj_path) if rd.is_file(pj_path) else rel(pd),
                      default=True if (pname in default_pages or pd.name in default_pages) else None)
            if pid:
                edge(app_id, pid, "contains")

        for pd, pname, page in page_meta:
            pid = page_ids[pname]
            if pid not in nodes:
                continue

            # -- collect the page's entities (names first, links second) --------------------
            # widgets: (widget dict, source file)
            raw_widgets: list[tuple[dict, Path]] = []
            layouts = page.get("layouts")
            for lay in layouts if isinstance(layouts, list) else []:
                dsl = lay.get("dsl") if isinstance(lay, dict) else None
                if isinstance(dsl, dict):
                    buf: list = []
                    _widgets_in(dsl, buf, _MAX_WIDGETS_PER_FILE)
                    pf = pd / f"{pd.name}.json"
                    raw_widgets.extend((w, pf) for w in buf)
            wdir = pd / "widgets"
            if rd.is_dir(wdir):
                for f in rd.tree(wdir, ".json", exclude, include):
                    wj = rd.json(f)
                    if isinstance(wj, dict):
                        buf = []
                        _widgets_in(wj, buf, _MAX_WIDGETS_PER_FILE)
                        raw_widgets.extend((w, f) for w in buf)

            by_wid: dict[str, dict] = {}            # widgetId -> widget, canvases included
            widgets: dict[str, tuple[dict, Path]] = {}  # name -> (widget, file), canvases excluded
            file_owner: dict[Path, str] = {}       # widgets/<X>/<X>.json -> X
            for w, f in raw_widgets:
                wid = w.get("widgetId")
                if isinstance(wid, str):
                    by_wid.setdefault(wid, w)
                if w.get("type") == _CANVAS:
                    continue
                name = w["widgetName"].strip()
                widgets.setdefault(name, (w, f))
                if f.stem == f.parent.name and f.stem == name:
                    file_owner[f.parent] = name

            queries: dict[str, tuple[dict, list[Path]]] = {}   # name -> (action, files to scan)
            qdir = pd / "queries"
            if rd.is_dir(qdir):
                for sub in rd.subdirs(qdir, exclude, include):        # format v5: one dir per query
                    meta = rd.json(sub / "metadata.json")
                    if not isinstance(meta, dict):
                        continue
                    act = _action(meta)
                    name = (_str(act.get("name")) or sub.name).strip()
                    queries.setdefault(name, (act, [sub / "metadata.json"] + rd.files(sub, ".txt")))
                for f in rd.files(qdir, ".json"):                     # older flat form
                    meta = rd.json(f)
                    if not isinstance(meta, dict):
                        continue
                    act = _action(meta)
                    name = (_str(act.get("name")) or f.stem).strip()
                    txt = f.with_suffix(".txt")
                    queries.setdefault(name, (act, [f] + ([txt] if rd.is_file(txt) else [])))

            jsobjs: dict[str, tuple[Path, Path]] = {}  # name -> (source file, js body)
            jdir = pd / "jsobjects"
            if rd.is_dir(jdir):
                for sub in rd.subdirs(jdir, exclude, include):
                    js = sub / f"{sub.name}.js"
                    if not rd.is_file(js):
                        cands = rd.files(sub, ".js")
                        if len(cands) != 1:
                            continue
                        js = cands[0]
                    meta = rd.json(sub / "metadata.json")
                    act = _action(meta) if isinstance(meta, dict) else {}
                    name = (_str(act.get("name")) or sub.name).strip()
                    jsobjs.setdefault(name, (sub / "metadata.json" if rd.is_file(sub / "metadata.json")
                                             else js, js))
                for js in rd.files(jdir, ".js"):
                    jsobjs.setdefault(js.stem, (js, js))

            # One namespace per page, as Appsmith enforces it.
            ids: dict[str, str] = {}
            kinds: dict[str, str] = {}
            for name in widgets:
                ids[name], kinds[name] = f"appsmithwidget:{app}/{pname}/{name}", "widget"
            for name in jsobjs:
                ids[name], kinds[name] = f"appsmithjs:{app}/{pname}/{name}", "js"
            for name in queries:
                ids[name], kinds[name] = f"appsmithquery:{app}/{pname}/{name}", "query"

            # -- nodes -----------------------------------------------------------------------
            for name, (act, files) in sorted(queries.items()):
                cfg = act.get("actionConfiguration")
                cfg = cfg if isinstance(cfg, dict) else {}
                dsref = act.get("datasource") if isinstance(act.get("datasource"), dict) else {}
                on_load = act.get("runBehaviour") == "ON_PAGE_LOAD" or act.get("executeOnLoad") is True
                qid = add(ids[name], label=name, type="appsmith_query", file_type="appsmith",
                          source_file=rel(files[0]),
                          plugin=_str(act.get("pluginId")) or _str(dsref.get("pluginId")),
                          plugin_type=_str(act.get("pluginType")) or _str(dsref.get("pluginType")),
                          http_method=_str(cfg.get("httpMethod")),
                          path=_path_only(cfg.get("path")),
                          on_page_load=True if on_load else None)
                if not qid:
                    continue
                edge(pid, qid, "contains")
                ds = act.get("datasource")
                if isinstance(ds, dict):
                    dname = _str(ds.get("name")) or _str(ds.get("id"))
                    dname = dname.strip() if dname else None
                    if dname and dname not in _EMBEDDED_DS and (ds.get("id") or dname in ds_ids):
                        did = ds_ids.get(dname) or add(
                            f"appsmithdatasource:{app}/{dname}", label=dname,
                            type="appsmith_datasource", file_type="appsmith",
                            source_file=rel(files[0]), plugin=_str(ds.get("pluginId")))
                        if did:
                            ds_ids.setdefault(dname, did)
                            edge(qid, did, "uses_datasource")

            for name, (src, js) in sorted(jsobjs.items()):
                jid = add(ids[name], label=name, type="appsmith_jsobject", file_type="appsmith",
                          source_file=rel(src))
                if jid:
                    edge(pid, jid, "contains")
                    # the exact node treesitter.py writes for the file, so the target is typed
                    # under [graph-slim] too and the two merge cleanly when both run
                    jrel = rel(js)
                    mid = add(f"module:{jrel}", label=jrel, type="module",
                              file_type=EXT_LANG.get(js.suffix.lower(), "javascript"),
                              source_file=jrel)
                    if mid:
                        edge(jid, mid, "implemented_by")

            for name, (w, f) in sorted(widgets.items()):
                add(ids[name], label=name, type="appsmith_widget", file_type="appsmith",
                    source_file=rel(f), widget_type=_str(w.get("type")))

            # nesting: walk parentId up through canvases; fall back to the directory owner
            for name, (w, f) in sorted(widgets.items()):
                wnid = ids[name]
                if wnid not in nodes:
                    continue
                parent = None
                cur, hops = w.get("parentId"), 0
                cur = cur if isinstance(cur, str) else None
                while cur is not None and cur in by_wid and hops < 64:
                    pw = by_wid[cur]
                    if pw.get("type") != _CANVAS and _str(pw.get("widgetName")):
                        parent = pw["widgetName"].strip()
                        break
                    cur, hops = pw.get("parentId"), hops + 1
                    cur = cur if isinstance(cur, str) else None
                if parent is None and cur is not None and cur not in by_wid and cur != "0":
                    owner = file_owner.get(f.parent)
                    if owner and owner != name:
                        parent = owner
                if parent and parent in widgets and parent != name and ids[parent] in nodes:
                    edge(ids[parent], wnid, "contains")
                else:
                    edge(pid, wnid, "contains")

            # -- links -----------------------------------------------------------------------
            names = set(ids)

            def link(src_id: str, code: str, trigger: bool):
                if not code:
                    return
                for m in _NAVIGATE.finditer(code):
                    target = page_ids.get(m.group(2).strip())
                    if target and target in nodes:
                        edge(src_id, target, "navigates_to")
                hits = {m.group(0) for m in _IDENT.finditer(code)} & names
                for t in sorted(hits):
                    tid = ids[t]
                    if tid == src_id or tid not in nodes:
                        continue
                    e = re.escape(t)
                    strong = False
                    if trigger and kinds[t] == "query" and re.search(
                            rf"(?<![\w$.]){e}\s*\.\s*run\s*\(", code):
                        edge(src_id, tid, "triggers")
                        strong = True
                    if trigger and kinds[t] == "js" and re.search(
                            rf"(?<![\w$.]){e}\s*\.\s*[A-Za-z_$][\w$]*\s*\(", code):
                        edge(src_id, tid, "triggers")
                        strong = True
                    if kinds[t] == "query" and re.search(
                            rf"(?<![\w$.]){e}\s*\.\s*data\b(?!\s*\()", code):
                        edge(src_id, tid, "reads")
                        strong = True
                    if not strong:
                        edge(src_id, tid, "references", "INFERRED")

            for name, (w, _f) in sorted(widgets.items()):
                wnid = ids[name]
                if wnid not in nodes:
                    continue
                trig = w.get("dynamicTriggerPathList")
                trig_keys = {d["key"] for d in (trig if isinstance(trig, list) else [])
                             if isinstance(d, dict) and isinstance(d.get("key"), str)}
                ev, other = [], []
                for k, v in w.items():
                    if k in _SKIP_WIDGET_KEYS:
                        continue
                    for path, s in _strings(v, k):
                        code = _bindings(s)
                        if not code:
                            continue
                        last = re.split(r"[.\[]", path)[-1]
                        (ev if path in trig_keys or _EVENT_KEY.match(last) else other).append(code)
                link(wnid, "\n".join(ev), trigger=True)
                link(wnid, "\n".join(other), trigger=False)

            for name, (act, files) in sorted(queries.items()):
                qid = ids[name]
                if qid not in nodes:
                    continue
                parts = [_bindings(s) for _, s in _strings(act.get("actionConfiguration") or {})]
                for f in files[1:]:
                    t = rd.text(f)
                    if t:
                        parts.append(_bindings(t))
                link(qid, "\n".join(p for p in parts if p), trigger=False)

            for name, (_src, js) in sorted(jsobjs.items()):
                jid = ids[name]
                if jid in nodes:
                    link(jid, rd.text(js) or "", trigger=True)

    for app_dir in _app_roots(root, rd, exclude, include):
        n_nodes, n_edges = len(nodes), len(edges)
        try:
            one_app(app_dir)
        except Exception:
            # An export shaped in a way this pass did not foresee costs that app, never the
            # build: drop what it had added so a half-read app does not reach the graph.
            for nid in list(nodes)[n_nodes:]:
                del nodes[nid]
            for e in edges[n_edges:]:
                seen_edges.discard((e["source"], e["target"], e["relation"]))
            del edges[n_edges:]

    return {"nodes": list(nodes.values()), "edges": edges}
