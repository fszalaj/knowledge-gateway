"""Azure Bicep extractor - the deployment graph a generic AST tool cannot see.

Bicep has no tree-sitter grammar in the slim install, and a line-by-line grep cannot tell a
`resource` declaration from the word inside a description string. This pass is a careful text
scanner instead: it blanks comments and string literals (keeping string interpolation code, so
`'${vault.name}'` still counts as a reference), tracks brace/bracket depth, and reads only the
line-anchored top-level declarations Bicep defines.

Read:
  *.bicep        targetScope, param, output, resource, module, import (and their decorators)
  *.bicepparam   using, extends, param <name> = ... (the name only)
  *.sh / *.bash / *.zsh / *.ps1 / *.psm1
                 `az deployment <scope> create|what-if|validate`, `az stack <scope> create`
                 and the `New-/Test-Az*Deployment` cmdlets, for their template and
                 parameter-file arguments

Emitted nodes:
  bicep:<rel>                 a .bicep (type bicep_template) or .bicepparam (bicep_params) file
  bicepresource:<rel>#<sym>   resource; resource_type, api_version, existing (nested children
                              read `bicepresource:<rel>#<parent>::<child>`)
  bicepmodule:<rel>#<sym>     module; `source` is the path or registry reference
  bicepparam:<rel>#<name>     param; param_type and the secure flag
  bicepoutput:<rel>#<name>    output; output_type
  extbicep:<ref>              a registry (br:, br/<alias>:) or template-spec (ts:) module
  kvsecret:<vault>/<secret>   a Key Vault secret read by name (`kvsecret:<secret>` when the
                              vault name is not a literal)
  module:<rel>                a deployment script, the same id the tree-sitter pass gives it

Emitted edges: declares (file -> its declarations), uses_module (module -> local bicep:<rel> or
extbicep:<ref>; a local file that does not exist gets a `missing` bicep:<rel> node, and a path
that leaves the root gets no edge), imports (file -> imported bicep file), parameterizes (.bicepparam -> the file
its `using` names), extends (.bicepparam -> base .bicepparam), sets_param (.bicepparam -> the
template param it assigns), depends_on (explicit dependsOn: EXTRACTED; any other symbolic
reference to a resource or module, directly or through a var: INFERRED), child_of (`parent:` and
nested resources), reads_secret (getSecret(...), @Microsoft.KeyVault(...) and Key Vault secret
URLs, by name) and deploys (script -> template and parameter file).

Deliberately absent: every VALUE. No param defaults, no .bicepparam assignments, no var bodies,
no property values, no descriptions, no tags - only names, types and the references between
declarations. Nothing is evaluated: no template compilation, no registry lookups, no network,
no subprocess. Variable names in deployment scripts are resolved only for the script's own
directory and plain literal assignments; anything else is left unresolved and produces no edge.
"""
from __future__ import annotations

import os
import posixpath
import re
import shlex
from bisect import bisect_left, bisect_right
from pathlib import Path

from .extract_ansible import skip_dir

MAX_BYTES = 2 * 1024 * 1024       # a larger .bicep is generated output, not something to read
MAX_DECLS = 2000                  # declarations per file
MAX_SECRETS = 500                 # secret references per file
_MAX_CALL_ARGS = 4096             # a getSecret(...) argument list longer than this is not read
_MAX_VALUE = 512                  # longest variable value a deployment script may define ...
_MAX_ENV = 256                    # ... how many it may define ...
_MAX_LINE = 64 * 1024             # ... and how far substitution may grow one line
_SCRIPT_LANG = {".sh": "bash", ".bash": "bash", ".zsh": "bash",
                ".ps1": "powershell", ".psm1": "powershell"}
_EXTERNAL = ("br:", "br/", "ts:", "ts/")

_DECL = re.compile(r"^[ \t]*(resource|module|param|output|var|targetScope|import|using|extends"
                   r"|type|func|metadata|extension|provider)\b", re.M)
_DECORATOR = re.compile(r"^[ \t]*@\s*(?:sys\s*\.\s*)?([A-Za-z_]\w*)", re.M)
_SYM = r"([A-Za-z_]\w*)"
_HEAD_RES = re.compile(r"\s+" + _SYM + r"\s+'")
_HEAD_PARAM = re.compile(r"\s+" + _SYM + r"(?:\s+([A-Za-z_][\w.]*(?:\[\])?))?")
_HEAD_NAME = re.compile(r"\s+" + _SYM)
_IDENT = re.compile(r"(?<![\w.$@#])([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\b")
# The lookbehind anchors the optional receiver at the start of an identifier; without it the
# engine retries the receiver from every character of a long identifier (quadratic).
_GETSECRET = re.compile(r"(?:(?<![\w:])([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*!?\s*\.\s*)?"
                        r"getSecret\s*\(")
_KV_NAME = re.compile(r"VaultName\s*=\s*([^;)]*)", re.I)
_KV_SECRET = re.compile(r"SecretName\s*=\s*([^;)]*)", re.I)
_KV_URI = re.compile(r"https://([A-Za-z0-9-]+)\.vault\.azure\.net/secrets/([A-Za-z0-9-]+)", re.I)
_KV_URL_KEY = re.compile(r"(?i)(keyvault\w*ur[il]|secretur[il])\s*:\s*$")
_KV_REL_SECRET = re.compile(r"(?:^|/)secrets/([A-Za-z0-9-]+)")
_NAME_PROP = re.compile(r"(?m)^[ \t]*name\s*:\s*'")
_TAIL = re.compile(r"\s*(existing\b\s*)?=")
_DEPENDS = re.compile(r"\bdependsOn\s*:\s*\[")
_PARENT = re.compile(r"(?m)^[ \t]*parent\s*:\s*([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)")


# --------------------------------------------------------------------------------------------
# scanner

def _scan(text: str):
    """Mask `text` for structural matching.

    Returns (code, strings). `code` has the same length as `text`: comments and the literal
    characters of every string are blanked to spaces (newlines kept), the quote characters stay,
    and interpolation code stays while its `${`/`}` delimiters are blanked - so bracket depth and
    identifiers can be read from `code` without a string or comment ever counting. `strings`
    maps each opening-quote offset to (literal, end): the literal with every interpolation
    replaced by NUL, and the offset just past the closing quote.
    """
    out = list(text)
    strings: dict[int, tuple[str, int]] = {}
    n = len(text)

    def blank(a, b):
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    # ["code", open-brace count inside an interpolation] or ["str", start, [chars]]
    stack: list[list] = [["code", 0]]
    i = 0
    while i < n:
        top = stack[-1]
        c = text[i]
        if top[0] == "str":
            if c == "\\" and i + 1 < n and text[i + 1] != "\n":
                top[2].append({"n": "\n", "r": "\r", "t": "\t"}.get(text[i + 1], text[i + 1]))
                blank(i, i + 2)
                i += 2
            elif c == "'":
                stack.pop()
                strings[top[1]] = ("".join(top[2]), i + 1)
                i += 1
            elif c == "$" and text.startswith("${", i):
                top[2].append("\x00")
                blank(i, i + 2)
                stack.append(["code", 0])
                i += 2
            elif c == "\n":   # Bicep strings are single-line; an unterminated one ends here
                stack.pop()
                strings[top[1]] = ("".join(top[2]), i)
                i += 1
            else:
                top[2].append(c)
                blank(i, i + 1)
                i += 1
            continue
        if c == "'":
            if text.startswith("'''", i):
                end = text.find("'''", i + 3)
                end = n if end < 0 else end
                strings[i] = (text[i + 3:end], min(end + 3, n))
                blank(i + 1, end + 3)
                if end + 2 < n:
                    out[end + 2] = "'"
                i = end + 3
            else:
                stack.append(["str", i, []])
                i += 1
            continue
        if c == "/" and text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
            continue
        if c == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(i, end)
            i = end
            continue
        if len(stack) > 1:      # inside an interpolation: its closing brace ends it
            if c == "{":
                top[1] += 1
            elif c == "}":
                if top[1] == 0:
                    blank(i, i + 1)
                    stack.pop()
                    i += 1
                    continue
                top[1] -= 1
        i += 1
    while len(stack) > 1:       # unterminated at end of file
        top = stack.pop()
        if top[0] == "str":
            strings[top[1]] = ("".join(top[2]), n)
    return "".join(out), strings


_BRACKETS = re.compile(r"[{}\[\]()\n]")


class _Doc:
    """Masked text plus depth lookups."""

    def __init__(self, text: str):
        self.text = text
        self.code, self.strings = _scan(text)
        self.string_starts = sorted(self.strings)
        self.line_starts = [0]
        self.line_depth = [0]
        # One pass precomputes what used to be rescanned per query: the bracket closing each
        # opener (`close`) and the running bracket balance (`_bpos`/`_bpre`, unclamped), so
        # match() and rel_depth() cost O(1)/O(log n) instead of a scan to the end of the file.
        self.close: dict[int, int] = {}
        self._bpos: list[int] = []
        self._bpre: list[int] = [0]
        opened: list[int] = []
        d = bal = 0
        for m in _BRACKETS.finditer(self.code):
            ch = m.group()
            if ch == "\n":
                self.line_starts.append(m.end())
                self.line_depth.append(d)
                continue
            self._bpos.append(m.start())
            if ch in "{[(":
                d += 1
                bal += 1
                opened.append(m.start())
            else:
                d = max(0, d - 1)
                bal -= 1
                if opened:
                    self.close[opened.pop()] = m.start()
            self._bpre.append(bal)

    def match(self, open_idx: int) -> int:
        """Index of the bracket closing the one at `open_idx` (len(code) if unbalanced) - the
        same answer as _match(), without the scan."""
        return self.close.get(open_idx, len(self.code))

    def rel_depth(self, a: int, b: int) -> int:
        """Openers minus closers in code[a:b] - the same answer as _rel_depth(), in O(log n)."""
        if b <= a:
            return 0
        return self._bpre[bisect_left(self._bpos, b)] - self._bpre[bisect_left(self._bpos, a)]

    def line_of(self, idx: int) -> int:
        return bisect_right(self.line_starts, idx)          # 1-based

    def depth_at(self, idx: int) -> int:
        k = bisect_right(self.line_starts, idx) - 1
        return self.line_depth[k] + self.rel_depth(self.line_starts[k], idx)

    def literal(self, quote_idx: int):
        """The string opening at `quote_idx` if it has no interpolation, else None."""
        s = self.strings.get(quote_idx)
        if s is None or "\x00" in s[0]:
            return None
        return s[0]


# _rel_depth / _match: the plain scans _Doc.rel_depth / _Doc.match replace. Unused by the pass,
# kept as the reference implementation the tests compare the precomputed lookups against.
def _rel_depth(code: str, a: int, b: int) -> int:
    d = 0
    for ch in code[a:b]:
        if ch in "{[(":
            d += 1
        elif ch in "}])":
            d -= 1
    return d


def _match(code: str, open_idx: int) -> int:
    """Index of the bracket closing the one at `open_idx` (len(code) if unbalanced)."""
    d = 0
    for m in _BRACKETS.finditer(code, open_idx):
        ch = m.group()
        if ch in "{[(":
            d += 1
        elif ch in "}])":
            d -= 1
            if d == 0:
                return m.start()
    return len(code)


def _body_open(code: str, start: int, limit: int):
    """The `{` opening a declaration body after its `=`: plain, `if (...) {` or `[for ...: {`."""
    i = start
    while i < limit and code[i] in " \t\r\n":
        i += 1
    if i < limit and code[i] == "[":
        i += 1
    d = 0
    while i < limit:
        ch = code[i]
        if ch == "{":
            if d == 0:
                return i
            d += 1
        elif ch in "([":
            d += 1
        elif ch in ")]}":
            d -= 1
            if d < 0:
                return None
        i += 1
    return None


def _call_args(doc: _Doc, open_paren: int):
    """Top-level arguments of the call whose `(` is at `open_paren`, as literals (None when an
    argument is not a plain string)."""
    code = doc.code
    close = doc.match(open_paren)
    if close - open_paren > _MAX_CALL_ARGS:
        return []           # unbalanced, or far longer than any literal secret reference
    pieces, d, start = [], 0, open_paren + 1
    for i in range(open_paren + 1, close):
        ch = code[i]
        if ch in "{[(":
            d += 1
        elif ch in "}])":
            d -= 1
        elif ch == "," and d == 0:
            pieces.append((start, i))
            start = i + 1
    pieces.append((start, close))
    args = []
    for a, b in pieces:
        seg = code[a:b]
        if not seg.strip().startswith("'"):
            args.append(None)
            continue
        q = a + len(seg) - len(seg.lstrip())
        lit = doc.literal(q)
        if lit is not None and doc.strings[q][1] != a + len(seg.rstrip()):
            lit = None          # a string followed by more expression is not a literal
        args.append(lit)
    return args


# --------------------------------------------------------------------------------------------
# paths

def _local_ref(base_dir: str, ref: str):
    """Normalise a path written in a .bicep/.bicepparam relative to its directory. None when it
    is not a plain local path or would leave the repository."""
    if not ref or "\x00" in ref or ref.startswith(_EXTERNAL) or "://" in ref:
        return None
    ref = ref.replace("\\", "/")
    if ref.startswith("/") or re.match(r"^[A-Za-z]:", ref):
        return None
    p = posixpath.normpath(posixpath.join(base_dir, ref))
    if p == ".." or p.startswith("../"):
        return None
    return p


def _contained_file(root_res: Path, rel: str) -> bool:
    try:
        p = (root_res / rel).resolve()
        p.relative_to(root_res)
        return p.is_file()
    except (OSError, ValueError, RuntimeError):
        return False


def _read(p: Path, root_res: Path):
    """File text, or None when it escapes the root, is too large or cannot be read."""
    try:
        rp = p.resolve()
        rp.relative_to(root_res)
        if not rp.is_file() or rp.stat().st_size > MAX_BYTES:
            return None
        return rp.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError, RuntimeError):
        return None


# --------------------------------------------------------------------------------------------
# .bicep / .bicepparam

def _decls(doc: _Doc):
    """Declarations: dicts with kind, name, start (line start), head (keyword end), depth."""
    out = []
    for m in _DECL.finditer(doc.code):
        out.append({"kind": m.group(1), "start": m.start(), "head": m.end(),
                    "depth": doc.depth_at(m.start(1))})
    return out


def _parse_file(doc: _Doc, rel: str, is_params: bool):
    """Structure of one file; no node ids yet. Everything value-shaped stays behind."""
    code = doc.code
    n = len(code)
    found = _decls(doc)
    top = [d for d in found if d["depth"] == 0]
    decorators = [(m.start(), m.group(1).lower()) for m in _DECORATOR.finditer(code)
                  if doc.depth_at(m.start(1)) == 0]

    info = {"target_scope": None, "using": None, "extends": None, "imports": [],
            "decls": [], "params_set": []}
    # extents of top-level declarations: up to the next one
    for k, d in enumerate(top):
        d["end"] = top[k + 1]["start"] if k + 1 < len(top) else n
    # a decorator belongs to the first declaration after it
    di = 0
    for d in top:
        decs = set()
        while di < len(decorators) and decorators[di][0] < d["start"]:
            decs.add(decorators[di][1])
            di += 1
        d["decorators"] = decs

    def string_after(pos, pattern):
        m = pattern.match(code, pos)
        if not m:
            return None, None, None
        q = m.end() - 1
        return m, doc.literal(q), doc.strings.get(q, (None, q + 1))[1]

    resources: list[dict] = []
    for d in top:
        kind, head, end = d["kind"], d["head"], d["end"]
        line = doc.line_of(d["start"])
        if kind == "targetScope":
            m = re.compile(r"\s*=\s*'").match(code, head)
            if m:
                info["target_scope"] = doc.literal(m.end() - 1)
        elif kind in ("using", "extends"):
            m = re.compile(r"\s+'").match(code, head)
            if m:
                lit = doc.literal(m.end() - 1)
                if lit is not None:
                    info[kind] = lit
        elif kind == "import":
            m = re.compile(r"\bfrom\s+'").search(code, head, end)
            if m:
                lit = doc.literal(m.end() - 1)
                if lit is not None:
                    info["imports"].append(lit)
        elif kind == "param" and is_params:
            m = _HEAD_NAME.match(code, head)
            if m:
                info["params_set"].append((m.group(1), d["start"], end))
        elif kind == "param":
            m = _HEAD_PARAM.match(code, head)
            if m:
                info["decls"].append({"kind": "param", "name": m.group(1), "line": line,
                                      "param_type": m.group(2),
                                      "secure": "secure" in d["decorators"],
                                      "start": d["start"], "end": end})
        elif kind == "output":
            m = _HEAD_PARAM.match(code, head)
            if m:
                info["decls"].append({"kind": "output", "name": m.group(1), "line": line,
                                      "output_type": m.group(2), "start": d["start"], "end": end})
        elif kind == "var":
            m = _HEAD_NAME.match(code, head)
            if m:
                info["decls"].append({"kind": "var", "name": m.group(1), "line": line,
                                      "start": d["start"], "end": end})
        elif kind in ("resource", "module"):
            m, lit, after = string_after(head, _HEAD_RES)
            if not m:
                continue
            rec = {"kind": kind, "name": m.group(1), "line": line, "ref": lit,
                   "start": d["start"], "end": end, "parent": None}
            tail = _TAIL.match(code, after or n)
            rec["existing"] = bool(tail and tail.group(1))
            if tail:
                eq = tail.end()
                rest = code[eq:min(end, eq + 200)].lstrip()
                rec["loop"] = rest.startswith("[")
                rec["conditional"] = bool(re.match(r"\[?\s*(?:for\b[^:]*:\s*)?if\b", rest))
                rec["body"] = _body_open(code, eq, end)
            else:
                rec["loop"] = rec["conditional"] = False
                rec["body"] = None
            info["decls"].append(rec)
            if kind == "resource":
                resources.append(rec)

    # nested resources: a `resource` line directly inside another resource's body
    inner = [d for d in found if d["kind"] == "resource" and d["depth"] > 0]
    inner_starts = [d["start"] for d in inner]          # already in file order

    def nest(parents):
        new = []
        for par in parents:
            b = par.get("body")
            if b is None:
                continue
            close = doc.match(b)
            # only the declarations inside this body (bisect), not every one in the file
            for d in inner[bisect_right(inner_starts, b):bisect_left(inner_starts, close)]:
                if doc.rel_depth(b + 1, d["start"]) != 0:
                    continue
                m, lit, after = string_after(d["head"], _HEAD_RES)
                if not m:
                    continue
                tail = _TAIL.match(code, after or n)
                body = _body_open(code, tail.end(), close) if tail else None
                cend = (doc.match(body) + 1) if body is not None else close
                rec = {"kind": "resource", "name": f"{par['name']}::{m.group(1)}",
                       "line": doc.line_of(d["start"]), "ref": lit, "start": d["start"],
                       "end": cend, "parent": par, "existing": bool(tail and tail.group(1)),
                       "loop": False, "conditional": False, "body": body}
                info["decls"].append(rec)
                new.append(rec)
        return new

    level = resources
    for _ in range(8):           # Bicep nests a handful of levels at most
        level = nest(level)
        if not level:
            break
    return info


def _spans_minus(start, end, holes):
    """[start, end) with the nested `holes` cut out."""
    spans, cur = [], start
    for a, b in sorted(holes):
        if b <= cur or a >= end:
            continue
        if a > cur:
            spans.append((cur, a))
        cur = max(cur, b)
    if cur < end:
        spans.append((cur, end))
    return spans


def _refs(doc: _Doc, spans, symbols, skip=()):
    """Symbols referenced in `spans` of masked code. An identifier that is an object key
    (followed by a single colon at the start of a line or after `{`/`,`) is not a reference."""
    code = doc.code
    got: list[str] = []
    for a, b in spans:
        for m in _IDENT.finditer(code, a, b):
            tok = m.group(1)
            parts = tok.split("::")
            # longest declared prefix: `vnet::subnet.id` is the subnet, `vnet.id` the vnet
            name = None
            for k in range(len(parts), 0, -1):
                cand = "::".join(parts[:k])
                if cand in symbols:
                    name = cand
                    break
            if name is None or name in skip:
                continue
            after = code[m.end():m.end() + 3].lstrip()
            if after.startswith(":") and not after.startswith("::"):
                before = code[max(a, m.start() - 200):m.start()].rstrip(" \t")
                if not before or before[-1] in "\n{,":
                    continue
            got.append(name)
    return got


def _secret_refs(doc: _Doc, spans, literal_names):
    """(vault or None, secret) pairs read in `spans`."""
    code = doc.code
    out = []
    for a, b in spans:
        for m in _GETSECRET.finditer(code, a, b):
            recv = m.group(1)
            args = _call_args(doc, m.end() - 1)
            if recv in (None, "az"):
                if len(args) >= 4 and args[3]:
                    out.append((args[2], args[3]))
            elif args and args[0]:
                out.append((literal_names.get(recv), args[0]))
        lo = bisect_left(doc.string_starts, a)
        hi = bisect_left(doc.string_starts, b)
        for q in doc.string_starts[lo:hi]:
            lit = doc.strings[q][0]
            if "@Microsoft.KeyVault" in lit:
                sn = _KV_SECRET.search(lit)
                vn = _KV_NAME.search(lit)
                if sn and "\x00" not in sn.group(1) and sn.group(1).strip():
                    vault = vn.group(1).strip() if vn and "\x00" not in vn.group(1) else None
                    out.append((vault or None, sn.group(1).strip()))
            for um in _KV_URI.finditer(lit):
                out.append((um.group(1), um.group(2)))
            if not _KV_URI.search(lit) and _KV_URL_KEY.search(code[max(a, q - 80):q]):
                rm = _KV_REL_SECRET.search(lit)
                if rm:
                    out.append((None, rm.group(1)))
    return out


def _literal_name(doc: _Doc, rec) -> str | None:
    """The literal `name:` of a resource body - used only to name the vault a
    `vault.getSecret('x')` call reads from."""
    b = rec.get("body")
    if b is None:
        return None
    close = doc.match(b)
    for m in _NAME_PROP.finditer(doc.code, b, close):
        if doc.rel_depth(b + 1, m.start()) == 0:
            return doc.literal(m.end() - 1)
    return None


# --------------------------------------------------------------------------------------------
# deployment scripts

_SD = "__KG_SCRIPT_DIR__"
_DIRNAME = re.compile(r'\$\(\s*dirname\s+(?:--\s+)?"?\$\{?(?:0|BASH_SOURCE(?:\[0\])?)\}?"?\s*\)')
_CD_PWD = re.compile(r'\$\(\s*cd\s+(?:--\s+)?"?' + _SD + r'"?\s*(?:&&|;)\s*pwd(?:\s+-P)?\s*\)')
_PS_ROOT = re.compile(r"\$(?:\{PSScriptRoot\}|PSScriptRoot\b)", re.I)
_PS_SPLIT = re.compile(r"\(?\s*Split-Path\s+(?:-Parent\s+)?(?:-Path\s+)?"
                       r"\$MyInvocation\.MyCommand\.(?:Path|Definition)(?:\s+-Parent)?\s*\)?", re.I)
_PS_JOIN = re.compile(r"\(?\s*Join-Path\s+(?:-Path\s+)?(\"[^\"]*\"|'[^']*'|\S+)\s+"
                      r"(?:-ChildPath\s+)?(\"[^\"]*\"|'[^']*'|[^\s)]+)\s*\)?", re.I)
_ASSIGN_SH = re.compile(r"^\s*(?:export\s+|local\s+|readonly\s+|declare\s+(?:-\w+\s+)?)?"
                        r"([A-Za-z_]\w*)=(.*)$")
_ASSIGN_PS = re.compile(r"^\s*\$([A-Za-z_]\w*)\s*=\s*(.*)$")
_VAR = re.compile(r"\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)")
_PS_CMDLET = re.compile(r"(?i)^(new|test|get)-az\w*deployment(?:stack|whatifresult)?$")
_AZ_ACTIONS = {"create": "create", "what-if": "what-if", "validate": "validate"}
_STOP = {"&&", "||", ";", "|", ">", ">>", "2>", "2>&1"}


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _strip_block_comments(text: str) -> str:
    """PowerShell `<# ... #>` comments removed by a linear scan: an unclosed `<#` ends the scan
    for good, where a lazy `<#.*?#>` regex rescans the rest of the file from every later `<#`."""
    out, pos = [], 0
    while True:
        a = text.find("<#", pos)
        if a < 0:
            break
        b = text.find("#>", a + 2)
        if b < 0:
            break
        out += [text[pos:a], " "]
        pos = b + 2
    out.append(text[pos:])
    return "".join(out)


def _logical_lines(text: str, ps: bool):
    if ps:
        text = _strip_block_comments(text)
        text = re.sub(r"`[ \t]*\r?\n", " ", text)
    else:
        text = re.sub(r"\\[ \t]*\r?\n", " ", text)
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        yield line


def _substitute(line: str, ps: bool, env: dict) -> str:
    line = _DIRNAME.sub(_SD, line)
    line = _CD_PWD.sub(_SD, line)
    if ps:
        line = _PS_SPLIT.sub(_SD, line)
        line = _PS_ROOT.sub(_SD, line)
        line = _PS_JOIN.sub(lambda m: _unquote(m.group(1)) + "/" + _unquote(m.group(2)), line)

    grown = [0]

    def var(m):
        name = m.group(1) or m.group(2)
        key = name.lower() if ps else name
        val = env.get(key)
        # a line may grow by at most _MAX_LINE through substitution; past that, references
        # stay unexpanded (and unresolvable), so `$A$A$A...` cannot balloon a line
        if val is None or grown[0] + len(val) > _MAX_LINE:
            return m.group(0)
        grown[0] += len(val)
        return val
    return _VAR.sub(var, line)


def _deploy_commands(text: str, ps: bool):
    """(operation, template or None, [parameter files]) per deployment command."""
    env: dict[str, str] = {}
    out = []
    for raw in _logical_lines(text, ps):
        line = _substitute(raw, ps, env)
        am = (_ASSIGN_PS if ps else _ASSIGN_SH).match(line)
        if am and not re.search(r"\s(?:az|New-Az|Test-Az)", line):
            val = _unquote(am.group(2).strip().rstrip(";").strip())
            key = am.group(1).lower() if ps else am.group(1)
            # Bounded: `B=$A$A` stores the expanded value, so without a cap each such line
            # would double the stored string (exponential memory from a few hundred bytes).
            if (val and len(val) <= _MAX_VALUE and "$" not in val and "`" not in val
                    and not re.search(r"\s", val) and (key in env or len(env) < _MAX_ENV)):
                env[key] = val
            continue
        if ps:
            line = line.replace("\\", "/")
        try:
            lex = shlex.shlex(line, posix=True)
            lex.whitespace_split = True
            lex.commenters = ""
            toks = list(lex)
        except ValueError:
            continue
        for i, t in enumerate(toks):
            op = None
            j = None
            t = t.rsplit("(", 1)[-1]           # `out=$(az ...` and `(az ...`
            if t == "az" and i + 2 < len(toks) and toks[i + 1] in ("deployment", "stack"):
                a, b = toks[i + 2], toks[i + 3] if i + 3 < len(toks) else ""
                if toks[i + 1] == "deployment" and a in _AZ_ACTIONS:
                    op, j = _AZ_ACTIONS[a], i + 3
                elif a in ("group", "sub", "mg", "tenant") and b in _AZ_ACTIONS:
                    op, j = _AZ_ACTIONS[b], i + 4
            elif _PS_CMDLET.match(t):
                verb = t.split("-", 1)[0].lower()
                if verb == "get" and not t.lower().endswith("whatifresult"):
                    continue        # reading a past deployment's status deploys nothing
                op = {"new": "create", "test": "validate", "get": "what-if"}[verb]
                j = i + 1
            if op is None:
                continue
            template, params = None, []
            k = j
            while k < len(toks) and toks[k] not in _STOP:
                tok, low = toks[k], toks[k].lower()
                if low in ("--template-file", "-f", "-templatefile") and k + 1 < len(toks):
                    template = toks[k + 1]
                    k += 2
                    continue
                if low.startswith("--template-file="):
                    template = tok.split("=", 1)[1]
                elif low in ("-templateparameterfile",) and k + 1 < len(toks):
                    params.append(toks[k + 1])
                    k += 2
                    continue
                elif low in ("--parameters", "-p"):
                    k += 1
                    while k < len(toks) and not toks[k].startswith("-") and toks[k] not in _STOP:
                        params.append(toks[k])
                        k += 1
                    continue
                elif low == "-whatif" and op == "create":
                    op = "what-if"
                k += 1
            params = [p.lstrip("@") for p in params
                      if "=" not in p and p.lstrip("@").lower().endswith((".bicepparam", ".json"))]
            if template or params:
                out.append((op, template, params))
            break
    return out


def _script_path(root_res: Path, script_dir: str, ref: str):
    ref = ref.strip().strip("()").strip()
    ref = _unquote(ref).replace("\\", "/")
    if not ref or "$" in ref or "://" in ref:
        return None
    if ref.startswith(_SD):
        bases = [script_dir]
        ref = ref[len(_SD):].lstrip("/") or "."
    else:
        if ref.startswith("/") or re.match(r"^[A-Za-z]:", ref):
            return None
        bases = [script_dir, ""]       # cwd is unknown: the script's directory, then the root
    for base in bases:
        rel = posixpath.normpath(posixpath.join(base, ref)) if base else posixpath.normpath(ref)
        if rel == ".." or rel.startswith("../"):
            continue
        if _contained_file(root_res, rel):
            return rel
    return None


# --------------------------------------------------------------------------------------------

def extract(root: Path, exclude: frozenset[str] = frozenset(),
            include: frozenset[str] = frozenset()) -> dict:
    root = Path(root)
    try:
        root_res = root.resolve()
    except (OSError, RuntimeError):
        return {"nodes": [], "edges": []}
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}

    def add(nid, **attrs):
        cur = nodes.setdefault(nid, {"id": nid})
        for k, v in attrs.items():
            if v is not None and k not in cur:
                cur[k] = v
        return nid

    def edge(src, dst, rel, confidence="EXTRACTED", **extra):
        key = (src, dst, rel)
        cur = edges.get(key)
        if cur is None:
            edges[key] = {"source": src, "target": dst, "relation": rel,
                          "confidence": confidence, **extra}
        elif confidence == "EXTRACTED":
            cur["confidence"] = "EXTRACTED"

    bicep_files: list[str] = []
    param_files: list[str] = []
    scripts: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not skip_dir(d, exclude, include))
        for fn in sorted(filenames):
            rel = (Path(dirpath) / fn).relative_to(root).as_posix()
            low = fn.lower()
            if low.endswith(".bicep"):
                bicep_files.append(rel)
            elif low.endswith(".bicepparam"):
                param_files.append(rel)
            elif os.path.splitext(low)[1] in _SCRIPT_LANG:
                scripts.append((rel, _SCRIPT_LANG[os.path.splitext(low)[1]]))

    def module_target(base_dir, ref):
        if ref is None:
            return None
        if ref.startswith(_EXTERNAL):
            return add(f"extbicep:{ref}", label=ref, type="bicep_registry_module",
                       file_type="bicep")
        local = _local_ref(base_dir, ref)
        if local is None:
            # A path that leaves the graphed root (`../../shared/net.bicep` when a subfolder
            # of a monorepo is graphed), an absolute path or a URL: not a registry module, and
            # not a file of this graph - no edge, rather than a mislabelled external node.
            return None
        if local.lower().endswith(".json"):
            return add(f"armtemplate:{local}", label=posixpath.basename(local),
                       type="arm_template", file_type="json", source_file=local)
        if _contained_file(root_res, local):
            return f"bicep:{local}"         # the file's own parse writes the node
        # nothing will ever parse it: a typed node that says the file is missing, rather than
        # an untyped placeholder
        return add(f"bicep:{local}", label=posixpath.basename(local),
                   type="bicep_params" if local.lower().endswith(".bicepparam") else "bicep_template",
                   file_type="bicep", source_file=local, missing=True)

    def secret_node(vault, secret, rel):
        nid = f"kvsecret:{vault}/{secret}" if vault else f"kvsecret:{secret}"
        return add(nid, label=secret, type="key_vault_secret", file_type="bicep",
                   source_file=rel, vault=vault)

    template_params: dict[str, set[str]] = {}

    # ---- templates
    for rel in bicep_files:
        text = _read(root / rel, root_res)
        if text is None:
            continue
        try:
            doc = _Doc(text)
            info = _parse_file(doc, rel, is_params=False)
        except Exception:       # malformed beyond what the scanner tolerates: skip the file
            continue
        base = posixpath.dirname(rel)
        fid = add(f"bicep:{rel}", label=posixpath.basename(rel), type="bicep_template",
                  file_type="bicep", source_file=rel, target_scope=info["target_scope"])
        for imp in info["imports"]:
            tgt = module_target(base, imp)
            if tgt:
                edge(fid, tgt, "imports")

        decls = info["decls"][:MAX_DECLS]
        ids: dict[str, str] = {}       # resource/module symbol -> node id
        params: set[str] = set()
        for d in decls:
            loc = f"L{d['line']}"
            if d["kind"] == "resource":
                rtype = api = None
                ref = d["ref"]
                if ref is not None:
                    rtype, _, api = ref.rpartition("@") if "@" in ref else (ref, "", "")
                    par = d["parent"]
                    if par is not None and "/" not in rtype and par.get("rtype"):
                        rtype = f"{par['rtype']}/{rtype}"
                        api = api or par.get("api")
                d["rtype"], d["api"] = rtype or None, api or None
                nid = add(f"bicepresource:{rel}#{d['name']}", label=d["name"],
                          type="bicep_resource", file_type="bicep", source_file=rel,
                          source_location=loc, resource_type=d["rtype"], api_version=d["api"],
                          existing=d["existing"], conditional=d["conditional"] or None,
                          loop=d["loop"] or None)
                ids[d["name"]] = nid
                edge(fid, nid, "declares")
            elif d["kind"] == "module":
                nid = add(f"bicepmodule:{rel}#{d['name']}", label=d["name"], type="bicep_module",
                          file_type="bicep", source_file=rel, source_location=loc,
                          source=d["ref"], conditional=d["conditional"] or None,
                          loop=d["loop"] or None)
                ids[d["name"]] = nid
                edge(fid, nid, "declares")
                tgt = module_target(base, d["ref"])
                if tgt:
                    edge(nid, tgt, "uses_module")
            elif d["kind"] == "param":
                nid = add(f"bicepparam:{rel}#{d['name']}", label=d["name"], type="bicep_param",
                          file_type="bicep", source_file=rel, source_location=loc,
                          param_type=d["param_type"], secure=d["secure"])
                params.add(d["name"])
                edge(fid, nid, "declares")
            elif d["kind"] == "output":
                nid = add(f"bicepoutput:{rel}#{d['name']}", label=d["name"], type="bicep_output",
                          file_type="bicep", source_file=rel, source_location=loc,
                          output_type=d["output_type"])
                edge(fid, nid, "declares")
        template_params[rel] = params

        # references between declarations
        symbols = set(ids)
        varnames = {d["name"] for d in decls if d["kind"] == "var"}
        lookup = symbols | varnames
        nested_spans = {id(d): [(c["start"], c["end"]) for c in decls if c.get("parent") is d]
                        for d in decls}
        direct: dict[str, list[str]] = {}
        spans_of: dict[int, list] = {}
        for d in decls:
            spans = _spans_minus(d["start"], d["end"], nested_spans.get(id(d), []))
            spans_of[id(d)] = spans
            # A declaration does not depend on itself. Outputs live in their own namespace, so
            # `output store string = store.name` is a real reference and is kept.
            own = () if d["kind"] == "output" else {d["name"]}
            direct[id(d)] = _refs(doc, spans, lookup, skip=own)
        var_refs = {d["name"]: direct[id(d)] for d in decls if d["kind"] == "var"}

        def expand(names):
            seen, out, stack = set(), [], list(names)
            while stack:
                x = stack.pop()
                if x in seen:
                    continue
                seen.add(x)
                if x in varnames:
                    stack.extend(var_refs.get(x, ()))
                elif x in symbols:
                    out.append(x)
            return out

        literal_names = {d["name"]: _literal_name(doc, d) for d in decls
                         if d["kind"] == "resource"}
        for d in decls:
            if d["kind"] not in ("resource", "module", "output"):
                continue
            src = (ids.get(d["name"]) if d["kind"] != "output"
                   else f"bicepoutput:{rel}#{d['name']}")
            if src is None:
                continue
            own = None if d["kind"] == "output" else d["name"]
            explicit: set[str] = set()
            parent_sym = None
            body = d.get("body")
            if body is not None and d["kind"] != "output":
                close = doc.match(body)
                for m in _DEPENDS.finditer(doc.code, body, close):
                    if doc.rel_depth(body + 1, m.start()) != 0:
                        continue
                    lb = m.end() - 1
                    explicit.update(expand(_refs(doc, [(lb, doc.match(lb) + 1)], lookup)))
                for m in _PARENT.finditer(doc.code, body, close):
                    if doc.rel_depth(body + 1, m.start()) == 0:
                        if m.group(1) in ids:
                            parent_sym = m.group(1)
                        break
            if d.get("parent") is not None:
                parent_sym = d["parent"]["name"]
            if parent_sym and parent_sym in ids and d["kind"] == "resource":
                edge(src, ids[parent_sym], "child_of")
            for sym in sorted(explicit):
                if sym != own:
                    edge(src, ids[sym], "depends_on")
            for sym in sorted(set(expand(direct[id(d)]))):
                if sym in (own, parent_sym) or sym in explicit:
                    continue
                edge(src, ids[sym], "depends_on", "INFERRED")

        # secrets, owned by the innermost declaration that contains them (else the file)
        owners = sorted((d for d in decls if d["kind"] in ("resource", "module")),
                        key=lambda d: d["start"])
        count = 0
        for d in owners:
            for vault, secret in _secret_refs(doc, spans_of[id(d)], literal_names):
                if count >= MAX_SECRETS:
                    break
                count += 1
                edge(ids[d["name"]], secret_node(vault, secret, rel), "reads_secret")
        covered = [s for d in owners for s in spans_of[id(d)]]
        for vault, secret in _secret_refs(doc, _spans_minus(0, len(doc.code), covered),
                                          literal_names):
            if count >= MAX_SECRETS:
                break
            count += 1
            edge(fid, secret_node(vault, secret, rel), "reads_secret")

    # ---- parameter files
    uses: dict[str, str] = {}           # .bicepparam rel -> template node id
    for rel in param_files:
        text = _read(root / rel, root_res)
        if text is None:
            continue
        try:
            doc = _Doc(text)
            info = _parse_file(doc, rel, is_params=True)
        except Exception:
            continue
        base = posixpath.dirname(rel)
        fid = add(f"bicep:{rel}", label=posixpath.basename(rel), type="bicep_params",
                  file_type="bicep", source_file=rel)
        tgt = module_target(base, info["using"]) if info["using"] else None
        if tgt:
            uses[rel] = tgt
            edge(fid, tgt, "parameterizes")
        if info["extends"]:
            ext = module_target(base, info["extends"])
            if ext:
                edge(fid, ext, "extends")
        trel = tgt[len("bicep:"):] if tgt and tgt.startswith("bicep:") else None
        declared = template_params.get(trel, set()) if trel else set()
        count = 0
        for name, a, b in info["params_set"][:MAX_DECLS]:
            if name in declared:
                edge(fid, f"bicepparam:{trel}#{name}", "sets_param")
            for vault, secret in _secret_refs(doc, [(a, b)], {}):
                if count >= MAX_SECRETS:
                    break
                count += 1
                edge(fid, secret_node(vault, secret, rel), "reads_secret")

    # ---- deployment scripts
    for rel, lang in scripts:
        text = _read(root / rel, root_res)
        if text is None or not re.search(r"(?i)deployment", text):
            continue
        try:
            cmds = _deploy_commands(text, ps=(lang == "powershell"))
        except Exception:
            continue
        sdir = posixpath.dirname(rel)
        sid = f"module:{rel}"
        for op, template, params in cmds:
            targets = []
            if template:
                t = _script_path(root_res, sdir, template)
                if t and t.lower().endswith(".bicep"):
                    targets.append(f"bicep:{t}")
            for p in params:
                t = _script_path(root_res, sdir, p)
                if t and t.lower().endswith(".bicepparam"):
                    targets.append(f"bicep:{t}")
                    if not template and t in uses:
                        targets.append(uses[t])
            for tgt in targets:
                add(sid, label=rel, type="module", file_type=lang, source_file=rel)
                edge(sid, tgt, "deploys", operations=[op])
                ops = edges[(sid, tgt, "deploys")]["operations"]
                if op not in ops:
                    ops.append(op)
                    ops.sort()

    return {"nodes": list(nodes.values()), "edges": list(edges.values())}
