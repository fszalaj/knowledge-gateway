"""Jenkins extractor - which stages a pipeline runs, with which credentials, building, pushing and
deploying what.

A Jenkinsfile is Groovy, and the tree-sitter pass sees only Groovy: calls and closures. What a
pipeline means lives in a small DSL on top of it - `stage('Build') { steps { sh './build.sh' } }`
- and in the shell text inside its steps. This repo-level pass (stdlib + PyYAML, no tree-sitter)
reads:

  Jenkinsfile, Jenkinsfile.*, *.jenkinsfile, *.groovy, and any other text file whose name contains
  "jenkins" (case-insensitive) other than documentation (.md, .rst, .adoc, .html ..., which often
  carry example pipelines) - each accepted only when its code (comments and strings aside)
  holds a declarative `pipeline { ... }` or a scripted `node { ... }` block: at the top level,
  inside top-level wrapper calls (`timestamps { node { } }`, `podTemplate(...) { node(x) { } }`,
  `try { }`) or in a shared-library `def call(...) { }`.

The Groovy is tokenized by one linear scan that understands line and block comments, '...',
"..." with `${ }` / `$name` interpolation, ''' and \"\"\" triple-quoted strings and /slashy/
regexes, then folded into a tree of statements and `{ }` blocks (a `{` on the line after
`stage('x')` belongs to it, as in Groovy). Nothing is evaluated: an interpolation is a hole.

Emitted nodes:
  jenkinspipeline:<rel>               a pipeline file; style (declarative|scripted), agent (kind),
                                      environment and parameters (NAMES), triggers (kinds: cron,
                                      pollSCM, upstream...), tools (`<type>:<installation>`), post
                                      (conditions)
  jenkinsstage:<rel>#<stage path>     a stage; nested and parallel stages are `Outer/Inner`;
                                      parallel, matrix, agent, environment, tools, post attributes.
                                      A stage named by an interpolation reads `${}`
  jenkinscredential:<id>              a credentials id (the id is a name; nothing else is read)
  extimage:<name[:tag]>               an image, in the docker pass's format and attributes
  helmrelease:<namespace or ->/<name> a Helm release, in the k8s pass's format
  beanstalkenv:<application or ->/<env>  an Elastic Beanstalk environment, and beanstalkapp:<name>
                                      for a named application - the Beanstalk pass's ids, name
                                      rules and node shapes (extract_beanstalk.env_node)
  and, for local files, the id their own pass uses: module:<rel> (scripts and code),
  dockerfile:<rel>, project:/solution:<rel> (the manifests pass), file:<rel> otherwise.

Emitted edges:
  contains          pipeline -> stage, stage -> nested stage
  depends_on        stage -> the stage before it in the same sequence (EXTRACTED for declarative
                    `stages { }`, INFERRED for scripted code, where control flow may reorder them);
                    parallel branches are not ordered
  uses_credential   pipeline/stage -> credential: `credentials('<id>')` (environment), every
                    `credentialsId:` (withCredentials bindings, checkout, git...), `credentials:`
                    named arguments, sshagent, docker.withRegistry and registryCredentialsId;
                    `via` names the binding or step, `variable` the environment name it fills
  uses_image        agent docker image, docker.image('x').inside / withRun
  runs_script       (INFERRED) sh / bat / powershell / pwsh steps that run a local file - directly,
                    through bash|sh|python|node|pwsh -File|cmd /c|call, after `cd` and `dir()` -
                    and `load 'x.groovy'`; `step` attribute
  builds            agent dockerfile, `docker build [-f F] <ctx>`, docker.build(), and
                    `dotnet build|publish [<project>]` (or the one project in the directory)
  tests             (INFERRED) `dotnet test [<project>]` -> the project or solution it tests
  has_environment   beanstalkapp -> beanstalkenv, when a deploy names the application
  deploys           helm upgrade|install -> helmrelease (installs -> a local helmchart:<dir>);
                    `eb deploy <env>`, `aws elasticbeanstalk update-environment|create-environment`
                    (application from the same option or an earlier create-application-version)
                    and `dotnet eb deploy-environment` (-app/-env, else the `application` and
                    `environment` names of its aws-beanstalk-tools-defaults.json, INFERRED)
                    -> beanstalkenv
  pushes            docker push, `buildx build --push -t`, docker.image(x).push() / a docker.build
                    result's .push(tag), `aws ecr put-image --repository-name R --image-tag T`
  Step edges hang off the innermost stage (the pipeline outside stages) and carry `post` when
  they come from a post condition block.

Deliberately absent - a hard rule, not an omission: every VALUE. No environment values, no
parameter defaults or choices, no credential contents (credentials are ids only, never a
username, secret or variable value), no shell text, no headers, request bodies, URLs, hosts or
query strings (no URL is graphed at all: withRegistry's and checkout's URLs are dropped), no
`--set`/`--build-arg`/`--option-settings` values, no trigger specs (cron lines, tokens), no agent
labels or args, no other key of a beanstalk defaults file. Not graphed: the Groovy code itself
(the tree-sitter pass owns that), shared libraries (`@Library` is not fetched), `build job:`
triggers, `.elasticbeanstalk/` configuration, stages created in loops beyond their one `${}` id.
Paths are resolved inside the repository only (the workspace root, then the Jenkinsfile's
directory), never through a symlink that leaves it. A file that is oversized, binary or
unreadable is skipped; one that trips the pass in a way not foreseen costs that file, never the
build. Each file has a node, edge and work budget proportional to its size - every bracket search
and every filesystem lookup is charged to it, and lookups are capped per file - so hostile input
costs that file its remaining output, never unbounded time.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
from pathlib import Path

from .extract_ansible import skip_dir
from .extract_beanstalk import app_name as _eb_app, app_node as _eb_app_node
from .extract_beanstalk import env_name as _eb_env, env_node as _eb_env_node
from .extract_docker import _GLOBAL_VALUE_FLAGS, _PUSH_VALUE_FLAGS, file_node, parse_image, shell_commands
from .extract_k8s import _HELM_VALUE_FLAGS
from .extract_manifests import node_for as _manifest_node

_FT = "jenkins"
_MAX_BYTES = 2 * 1024 * 1024    # a bigger pipeline is generated or hostile: skip it
_MAX_TOKENS = 400_000           # Groovy tokens read from one file; the rest is ignored
_MAX_DEPTH = 64                 # nested { } blocks interpreted; deeper ones are skipped whole
_MAX_NODES = 2000               # new nodes one file may add
_MAX_EDGES = 5000               # edges one file may add
_MAX_NAMES = 200                # names one list attribute may hold
_MAX_SCRIPT = 256 * 1024        # characters of one sh/bat/powershell body that are parsed
_MAX_SCAN = 4096                # tokens one call's argument list is searched
_WORK_BASE = 200_000            # work one file may spend is this ...
_WORK_PER_BYTE = 8              # ... plus this x its size
_NAME_LEN = 120
_MAX_PATH = 1024                # a working directory or path argument longer than this is unknown
_MAX_PROBES = 1000              # filesystem lookups (stat / directory listing) one file may cause
_PROBE_COST = 500               # work one uncached lookup spends: a syscall costs more than a token
_DOC_EXT = (".md", ".markdown", ".mdx", ".rst", ".adoc", ".asciidoc", ".html", ".htm", ".xhtml",
            ".org", ".tex", ".ipynb")   # docs that merely mention Jenkins, often with examples

_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_NUMBER = re.compile(r"[0-9][\w.]*")
_DOLLAR_REF = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PARAM = re.compile(r"[A-Za-z_][\w.-]{0,99}")
# a credentials id: a slug or UUID. No `/` or `@`, so a URL or `user@host` is never taken for one
_CRED_ID = re.compile(r"\w[\w.:-]{0,127}")
_PLAIN = re.compile(r"[A-Za-z0-9][\w.-]{0,99}")
_K8S_NAME = re.compile(r"[A-Za-z0-9][\w.-]{0,62}")
_DIGEST_REF = re.compile(r"[^@\s]+@[A-Za-z0-9_+.-]+:[A-Fa-f0-9]{32,}")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_DRIVE = re.compile(r"[A-Za-z]:")
_PYTHON = re.compile(r"(?:python|pypy)(?:\d+(?:\.\d+)?)?")
_WS_PREFIX = re.compile(
    r"(?:\$\{?WORKSPACE\}?|%WORKSPACE%|\$env:WORKSPACE|\$\{env\.WORKSPACE\})(?:/|$)", re.I)
_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
_TWO_CHAR = frozenset({"==", "!=", "<=", ">=", "=~", "&&", "||", "->", "?.", "*.", "?:", "++",
                       "--", "+=", "-=", "<<", ">>"})
# After one of these a `/` starts a slashy string (a regex), elsewhere it divides.
_OPERAND_OPS = frozenset({"(", "[", ",", "=", ":", "!", "?", "{", ";", "+", "-", "*", "%", "&",
                          "|", "<", ">", "~", "==", "!=", "=~", "==~", "<=", ">=", "&&", "||",
                          "->", "?:"})
_OPERAND_IDS = frozenset({"return", "case", "in", "assert"})

_SHELL_STEPS = {"sh": "sh", "bat": "bat", "powershell": "powershell", "pwsh": "powershell"}
_PREFIX_WORDS = frozenset({"sudo", "exec", "time", "nohup", "command", "env", "then", "do", "else",
                           "if", "!", "{", "}", "&", "elif", "builtin"})
_SHELL_INTERP = frozenset({"bash", "sh", "zsh", "dash", "ksh", "ash", "source", ".", "node",
                           "ruby", "perl", "php", "groovy"})
_DOCKER_BINS = frozenset({"docker", "podman", "nerdctl"})
# the docker pass's value-taking flag sets, shared so a flag added there lands here too: a flag
# missing from them would make its value look like an image or release name
_DOCKER_GLOBAL_VALUE = frozenset(_GLOBAL_VALUE_FLAGS)
_DOCKER_PUSH_VALUE = frozenset(_PUSH_VALUE_FLAGS)
# docker build flags that consume the next word; a missing one only costs a missed edge (the
# context must resolve to a directory of the repository)
_DOCKER_BUILD_VALUE = frozenset({
    "-f", "--file", "-t", "--tag", "--build-arg", "--target", "--platform", "--label", "--secret",
    "--ssh", "--cache-from", "--cache-to", "--network", "--progress", "-o", "--output",
    "--iidfile", "--build-context", "--add-host", "--metadata-file", "--shm-size", "--ulimit",
    "--allow", "--annotation", "--attest", "--builder", "--cgroup-parent", "--isolation", "-m",
    "--memory", "--memory-swap", "--cpu-period", "--cpu-quota", "--cpu-shares", "-c",
    "--cpuset-cpus", "--cpuset-mems", "--security-opt", "--format", "--authfile", "--creds",
    "-v", "--volume", "-e", "--env", "--arch", "--os", "--variant", "--provenance", "--sbom"})
# helm install/upgrade value flags: the k8s pass's set (imported above), for the same reason
_DOTNET_VALUE = frozenset({"-c", "--configuration", "-f", "--framework", "-r", "--runtime", "-o",
                           "--output", "-v", "--verbosity", "-a", "--arch", "--os", "-s",
                           "--source", "--version-suffix", "--property", "-bl", "-p",
                           # dotnet test
                           "--filter", "-l", "--logger", "--results-directory", "--settings",
                           "--collect", "-d", "--diag", "--test-adapter-path", "-e",
                           "--environment", "--blame-hang-timeout", "--blame-hang-dump-type",
                           "--blame-crash-dump-type"})
_DOTNET_BUILDS = {"build": "builds", "publish": "builds", "test": "tests"}
_EB_VALUE = frozenset({"-l", "--label", "-m", "--message", "--version", "--timeout", "--source",
                       "-r", "--region", "--profile", "--modules", "-egs", "--env-group-suffix"})
_AWS_GLOBAL_VALUE = frozenset({"--region", "--profile", "--output", "--endpoint-url", "--query",
                               "--color", "--ca-bundle", "--cli-read-timeout",
                               "--cli-connect-timeout", "--cli-binary-format"})
_PROJECT_EXT = (".csproj", ".fsproj", ".vbproj")
_SOLUTION_EXT = (".sln", ".slnx")
_NOT_A_CALL_HEAD = frozenset({"class", "def", "interface", "enum", "trait"})
_METHOD_WORDS = frozenset({"def", "class", "static", "private", "public", "protected", "void",
                           "interface", "enum", "trait"})


class _Full(Exception):
    """The current file has spent its node, edge or work budget; keep what it produced."""


# --------------------------------------------------------------------------------------------
# file selection

def is_pipeline_name(name: str) -> bool:
    """A file whose name says it may be a pipeline; its content still has to agree."""
    n = name.lower()
    return (n == "jenkinsfile" or n.startswith("jenkinsfile.") or n.endswith(".jenkinsfile")
            or n.endswith(".groovy") or ("jenkins" in n and not n.endswith(_DOC_EXT)))


# --------------------------------------------------------------------------------------------
# Groovy tokens: (kind, value, line) with kind id | str | num | op | nl

def _placeholder(expr: str) -> str:
    # `${WORKSPACE}` is the checkout root, which is resolvable; every other hole is not
    return "$WORKSPACE" if expr.strip() in ("WORKSPACE", "env.WORKSPACE") else "${}"


def _interp(text: str, i: int, n: int, triple: bool) -> tuple[int, str]:
    """End of a `${ ... }` whose body starts at i, and the body (capped). Linear: nested quotes
    are skipped once; an unclosed hole ends at the line (one-line strings) or the file."""
    depth, start = 1, i
    while i < n:
        c = text[i]
        if c == "\n" and not triple:
            return i, ""
        if c in "'\"":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1, text[start:i][:64]
        i += 1
    return n, ""


def _string(text: str, i: int, n: int, q: str, triple: bool, interp: bool) -> tuple[str, int]:
    """Decoded string body from i (just past the opening quotes) and the index past its end.
    A one-line string left open ends at its line, so one stray quote cannot swallow the file."""
    out: list[str] = []
    close = q * 3 if triple else q
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            nx = text[i + 1]
            if nx != "\n":
                out.append(_ESC.get(nx, nx))
            i += 2
            continue
        if c == q and (not triple or text.startswith(close, i)):
            return "".join(out), i + len(close)
        if c == "\n" and not triple:
            return "".join(out), i
        if interp and c == "$" and i + 1 < n:
            if text[i + 1] == "{":
                i, expr = _interp(text, i + 2, n, triple)
                out.append(_placeholder(expr))
                continue
            m = _DOLLAR_REF.match(text, i + 1)
            if m:
                out.append(_placeholder(m.group(0)))
                i = m.end()
                continue
        out.append(c)
        i += 1
    return "".join(out), n


def _operand(prev) -> bool:
    if prev is None or prev[0] == "nl":
        return True
    if prev[0] == "op":
        return prev[1] in _OPERAND_OPS
    return prev[0] == "id" and prev[1] in _OPERAND_IDS


def _tokens(text: str) -> list[tuple]:
    out: list[tuple] = []
    i, n, line = 0, len(text), 1
    if text.startswith("#!"):
        j = text.find("\n")
        i = n if j < 0 else j
    prev = None
    no_slashy_before = -1     # a failed /slashy/ scan reached here: later tries on the line fail too
    while i < n and len(out) < _MAX_TOKENS:
        c = text[i]
        if c == "\n":
            out.append(("nl", "", line))
            prev = out[-1]
            line += 1
            i += 1
            continue
        if c in " \t\r\f\v﻿":
            i += 1
            continue
        if c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += text.count("\n", i, j)
            i = j
            continue
        if c in "'\"":
            triple = text.startswith(c * 3, i)
            start = i
            val, i = _string(text, i + (3 if triple else 1), n, c, triple, c == '"')
            out.append(("str", val, line))
            prev = out[-1]
            line += text.count("\n", start, i)
            continue
        if c == "/" and i > no_slashy_before and _operand(prev):
            j = i + 1
            while j < n and text[j] not in "/\n":
                j += 2 if text[j] == "\\" else 1
            if j < n and text[j] == "/":
                out.append(("str", "", line))      # a regex: its content is never needed
                prev = out[-1]
                line += text.count("\n", i, j)     # only escaped newlines get this far
                i = j + 1
                continue
            no_slashy_before = j
        m = _IDENT.match(text, i)
        if m:
            out.append(("id", m.group(0), line))
            prev = out[-1]
            i = m.end()
            continue
        if "0" <= c <= "9":
            m = _NUMBER.match(text, i)
            out.append(("num", m.group(0), line))
            prev = out[-1]
            i = m.end()
            continue
        if text.startswith("==~", i):
            op = "==~"
        elif text[i:i + 2] in _TWO_CHAR:
            op = text[i:i + 2]
        else:
            op = c
        out.append(("op", op, line))
        prev = out[-1]
        i += len(op)
    return out


def _tree(tokens: list[tuple]) -> list:
    """Statements (token lists) per block; a `{ }` block is a ("blk", [statements], line) token
    inside the statement that opens it. Newlines end statements outside brackets."""
    root: list = []
    stack = [[root, [], 0]]          # [statements, current statement, bracket depth]
    skipped = 0                      # depth inside a block nested past _MAX_DEPTH
    for tok in tokens:
        kind, val = tok[0], tok[1]
        f = stack[-1]
        if skipped:
            if kind == "op" and val == "{":
                skipped += 1
            elif kind == "op" and val == "}":
                skipped -= 1
            continue
        if kind == "op" and val == "{":
            if len(stack) > _MAX_DEPTH:
                skipped = 1
                continue
            if not f[1] and f[0]:
                last = f[0][-1]
                lt = last[-1]
                if ((lt[0] == "op" and lt[1] == ")") or (len(last) == 1 and lt[0] == "id")
                        or (lt[0] == "id" and lt[1] in ("else", "try", "finally"))):
                    f[1] = f[0].pop()        # `stage('x')` <newline> `{`: the block is its body
            child: list = []
            f[1].append(("blk", child, tok[2]))
            stack.append([child, [], 0])
            continue
        if kind == "op" and val == "}":
            if len(stack) == 1:
                continue                     # stray closer
            if f[1]:
                f[0].append(f[1])
            stack.pop()
            continue
        if kind == "nl" or (kind == "op" and val == ";"):
            if f[2] == 0 and f[1]:
                f[0].append(f[1])
                f[1] = []
            continue
        if kind == "op" and val in ("(", "["):
            f[2] += 1
        elif kind == "op" and val in (")", "]"):
            f[2] = max(0, f[2] - 1)
        f[1].append(tok)
    while stack:
        f = stack.pop()
        if f[1]:
            f[0].append(f[1])
    return root


# --------------------------------------------------------------------------------------------
# token helpers

def _at(toks, i):
    return toks[i] if 0 <= i < len(toks) else None


def _is(t, kind, val=None) -> bool:
    return t is not None and t[0] == kind and (val is None or t[1] == val)


def _close(toks, i) -> int:
    """Index of the bracket that closes toks[i] (an opening ( or [); when it is unclosed or
    farther than _MAX_SCAN tokens, the end of the window searched - so a slice up to it stays
    bounded however long the statement is."""
    depth = 0
    end = min(len(toks), i + _MAX_SCAN)
    for j in range(i, end):
        k, v = toks[j][0], toks[j][1]
        if k == "op":
            if v in ("(", "["):
                depth += 1
            elif v in (")", "]"):
                depth -= 1
                if depth == 0:
                    return j
    return end


def _named(toks, key):
    """The token after `key:` at the top level of an argument list."""
    depth = 0
    for j, t in enumerate(toks[:_MAX_SCAN]):
        if t[0] == "op":
            if t[1] in ("(", "["):
                depth += 1
            elif t[1] in (")", "]"):
                depth -= 1
        elif depth == 0 and t[0] == "id" and t[1] == key and _is(_at(toks, j + 1), "op", ":"):
            return _at(toks, j + 2)
    return None


def _positional(toks) -> list[str]:
    """String literals passed positionally at the top level of an argument list."""
    out, depth = [], 0
    for j, t in enumerate(toks[:_MAX_SCAN]):
        if t[0] == "op":
            if t[1] in ("(", "["):
                depth += 1
            elif t[1] in (")", "]"):
                depth -= 1
        elif depth == 0 and t[0] == "str":
            p = _at(toks, j - 1)
            if p is None or _is(p, "op", ","):
                out.append(t[1])
    return out


def _head(stmt):
    return stmt[0][1] if stmt and stmt[0][0] == "id" else None


def _blk(stmt):
    return next((t[1] for t in stmt if t[0] == "blk"), None)


def _first_str(stmt):
    for t in stmt:
        if t[0] == "blk":
            return None
        if t[0] == "str":
            return t[1]
    return None


def _call_head(head) -> bool:
    """`name`, `name(...)`, `a.b(...).c(...)` - a call whose trailing block is a closure."""
    if not head or head[0][0] != "id" or head[0][1] in _NOT_A_CALL_HEAD:
        return False
    i = 1
    while i < len(head):
        if _is(head[i], "op", "("):
            c = _close(head, i)
            if c >= len(head) or not _is(head[c], "op", ")"):
                return False
            i = c + 1
        elif _is(head[i], "op", ".") and _is(_at(head, i + 1), "id"):
            i += 2
        else:
            return False
    return True


def _style(block, depth: int = 0) -> str | None:
    """declarative / scripted when the file holds an entry block where Jenkins looks for one."""
    for stmt in block:
        name = _head(stmt)
        if name is None:
            continue
        bi = next((i for i, t in enumerate(stmt) if t[0] == "blk"), None)
        if bi is None:
            continue
        head = stmt[:bi]
        if name == "pipeline" and bi == 1:
            return "declarative"
        if name == "node" and _call_head(head):
            return "scripted"
        if depth < 3:
            lib = name == "def" and len(head) >= 2 and _is(head[1], "id", "call")
            if lib or _call_head(head):
                for t in stmt[bi:]:
                    if t[0] == "blk":
                        r = _style(t[1], depth + 1)
                        if r:
                            return r
    return None


def _stage_name(stmt) -> str:
    t = _at(stmt, 1)
    s = None
    if _is(t, "str"):
        s = t[1]
    elif _is(t, "op", "("):
        args = stmt[2:_close(stmt, 1)]
        v = _named(args, "name")
        if _is(v, "str"):
            s = v[1]
        elif args and args[0][0] == "str":
            s = args[0][1]
    s = " ".join(s.split())[:_NAME_LEN] if s else ""
    return s or "${}"


def _name_args(toks) -> list[str]:
    """Every `name: '<literal>'` in toks - parameter names; their defaults are never read."""
    out = []
    for j, t in enumerate(toks):
        if (t[0] == "id" and t[1] == "name" and _is(_at(toks, j + 1), "op", ":")
                and _is(_at(toks, j + 2), "str") and _PARAM.fullmatch(toks[j + 2][1])):
            out.append(toks[j + 2][1])
    return out


# --------------------------------------------------------------------------------------------
# shell helpers

def _join(base: str, p) -> str | None:
    """Relative path `p` from repo directory `base` as a repo-relative POSIX path ("" = root);
    None when it is not a literal local path or would leave the repository."""
    if not isinstance(p, str):
        return None
    p = p.strip().replace("\\", "/")
    if (not p or "$" in p or "%" in p or "{" in p or "://" in p or p.startswith(("/", "~"))
            or _DRIVE.match(p)):
        return None
    out = posixpath.normpath(posixpath.join(base, p) if base else p)
    if out == ".":
        return ""
    if out == ".." or out.startswith(("../", "/")):
        return None
    return out


def _prog(word: str) -> str:
    p = re.split(r"[\\/]", word)[-1].lower()
    return p[:-4] if p.endswith(".exe") else p


def _opts(words: list[str]) -> dict[str, str]:
    """`--flag value` / `--flag=value` pairs (keys lowercased). Only for tools whose positional
    arguments are not wanted, since a boolean flag would eat the next word."""
    out: dict[str, str] = {}
    i = 0
    while i < len(words):
        w = words[i]
        if w.startswith("-") and len(w) > 1:
            k, eq, v = w.partition("=")
            if not eq and i + 1 < len(words) and not words[i + 1].startswith("-"):
                v = words[i + 1]
                i += 1
            out[k.lower()] = v
        i += 1
    return out


def _image(ref):
    """parse_image, refusing anything that could carry credentials (`user:pw@host/x`, URLs)."""
    if not isinstance(ref, str) or "://" in ref:
        return None
    ref = ref.strip()
    if "@" in ref and not _DIGEST_REF.fullmatch(ref):
        return None
    return parse_image(ref)


def _parse_helm(args: list[str]):
    """(release, namespace, chart) of `helm upgrade|install`; every other value is dropped."""
    sub, pos, ns = None, [], None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos += args[i + 1:]
            break
        if a.startswith("-") and len(a) > 1:
            name, eq, val = a.partition("=")
            if name in _HELM_VALUE_FLAGS:
                if not eq:
                    i += 1
                    val = args[i] if i < len(args) else ""
                if name in ("-n", "--namespace"):
                    ns = val
            i += 1
            continue
        if sub is None:
            sub = a
        else:
            pos.append(a)
        i += 1
    if sub in ("upgrade", "install") and len(pos) >= 2:
        return pos[0], ns, pos[1]
    return None


def _dockerfile_node(rel: str) -> dict:
    # the node the docker pass writes for the same file
    return {"id": f"dockerfile:{rel}", "label": posixpath.basename(rel), "type": "dockerfile",
            "file_type": "docker", "source_file": rel}


# --------------------------------------------------------------------------------------------
# the pass

class _Ctx:
    """Where a statement sits: who owns its edges, where new stages hang, the stage sequence it
    extends, the working directory (None = unknown) and the post condition."""
    __slots__ = ("owner", "parent", "path", "seq", "conf", "base", "post", "par")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def but(self, **kw) -> "_Ctx":
        c = _Ctx(**{k: getattr(self, k) for k in self.__slots__})
        for k, v in kw.items():
            setattr(c, k, v)
        return c


class _Pass:
    def __init__(self, root: Path):
        self.root = root
        self.root_res = root.resolve()
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self.keys: list[tuple] = []
        self.seen: set[tuple] = set()
        self.kinds: dict[str, str | None] = {}
        self.probes = _MAX_PROBES

    # ---- budgets and output -----------------------------------------------------------------
    def begin(self, rel: str, size: int):
        self.rel = rel
        self.fdir = posixpath.dirname(rel)
        self.new_nodes: list[str] = []
        self.n_edges0 = len(self.edges)
        self.node_budget = _MAX_NODES
        self.edge_budget = _MAX_EDGES
        self.work = _WORK_BASE + _WORK_PER_BYTE * size
        self.probes = _MAX_PROBES
        self.vars: dict[str, dict] = {}       # Groovy variable -> image info (docker.build/image)
        self.apps: dict[str, str] = {}        # owner -> application of create-application-version
        self.pid = f"jenkinspipeline:{rel}"

    def charge(self, n: int):
        self.work -= n
        if self.work < 0:
            raise _Full

    def close(self, toks, i) -> int:
        """_close, charged for the tokens it searched. Every bracket search inside the pass goes
        through here, so no step can scan an argument list outside the file's work budget."""
        c = _close(toks, i)
        self.charge(c - i + 1)
        return c

    def probe(self) -> bool:
        """Spend one filesystem lookup: a fixed share of the work budget, and one of the file's
        _MAX_PROBES. False once they are gone - the path is then unknown, never looked up."""
        if self.probes <= 0:
            return False
        self.probes -= 1
        self.charge(_PROBE_COST)
        return True

    def add(self, n: dict) -> str:
        cur = self.nodes.get(n["id"])
        if cur is None:
            if self.node_budget <= 0:
                raise _Full
            self.node_budget -= 1
            self.nodes[n["id"]] = {k: v for k, v in n.items() if v is not None}
            self.new_nodes.append(n["id"])
        else:
            for k, v in n.items():
                if v is not None and k not in cur:
                    cur[k] = v
        return n["id"]

    def edge(self, src, dst, relation, confidence="EXTRACTED", **attrs):
        if src == dst:
            return
        attrs = {k: v for k, v in attrs.items() if v is not None}
        key = (src, dst, relation, tuple(sorted((k, str(v)) for k, v in attrs.items())))
        if key in self.seen:
            return
        if self.edge_budget <= 0:
            raise _Full
        self.edge_budget -= 1
        self.seen.add(key)
        self.keys.append(key)
        self.edges.append({"source": src, "target": dst, "relation": relation,
                           "confidence": confidence, **attrs})

    def step_edge(self, ctx: _Ctx, dst, relation, confidence="EXTRACTED", **attrs):
        self.edge(ctx.owner, dst, relation, confidence, post=ctx.post, **attrs)

    def names(self, nid: str, key: str, vals):
        node = self.nodes.get(nid)
        if node is None:
            return
        cur = node.get(key)
        cur = list(cur) if isinstance(cur, list) else []
        for v in vals:
            if v and v not in cur and len(cur) < _MAX_NAMES:
                cur.append(v)
        if cur:
            node[key] = cur

    def rollback(self):
        for nid in self.new_nodes:
            self.nodes.pop(nid, None)
        for key in self.keys[self.n_edges0:]:
            self.seen.discard(key)
        del self.keys[self.n_edges0:]
        del self.edges[self.n_edges0:]

    # ---- filesystem -------------------------------------------------------------------------
    def read(self, path: Path) -> str | None:
        try:
            real = path.resolve(strict=True)
            if not real.is_relative_to(self.root_res) or not real.is_file():
                return None
            if real.stat().st_size > _MAX_BYTES:
                return None
            return real.read_text(encoding="utf-8", errors="replace").lstrip("﻿")
        except (OSError, RuntimeError, ValueError):
            return None

    def kind(self, rel: str) -> str | None:
        """'file' / 'dir' for a path inside the repository (links resolved), else None."""
        if rel in self.kinds:
            return self.kinds[rel]
        if not self.probe():
            return None
        try:
            real = (self.root_res / rel).resolve(strict=True) if rel else self.root_res
            k = (None if not real.is_relative_to(self.root_res)
                 else "file" if real.is_file() else "dir" if real.is_dir() else None)
        except (OSError, RuntimeError, ValueError):
            k = None
        if len(self.kinds) < 100_000:
            self.kinds[rel] = k
        return k

    def subdir(self, cwd, arg):
        """The working directory after `dir(arg)` / `cd arg`; None when it cannot be known."""
        if not isinstance(arg, str):
            return None
        self.charge(len(arg) + len(cwd or ""))
        a = arg.strip().replace("\\", "/")
        m = _WS_PREFIX.match(a)
        if m:
            out = _join("", a[m.end():] or ".")
        elif cwd is None:
            return None
        else:
            out = _join(cwd, a)
        return out if out is None or len(out) <= _MAX_PATH else None

    def resolve(self, cwd, arg, want: str) -> str | None:
        """`arg` as a repo path of kind `want` ('file'/'dir'), from the working directory below
        the checkout root, then below the pipeline file's own directory."""
        if not isinstance(arg, str) or not arg.strip() or len(arg) > _MAX_PATH:
            return None
        self.charge(len(arg) + len(cwd or ""))
        a = arg.strip().replace("\\", "/")
        m = _WS_PREFIX.match(a)
        if m:
            a, bases, roots = a[m.end():] or ".", [""], [""]
        elif cwd is None:
            return None
        else:
            bases, roots = [cwd], list(dict.fromkeys(["", self.fdir]))
        for r in roots:
            for b in bases:
                d = _join(r, b) if b else r
                if d is None:
                    continue
                p = _join(d, a)
                if p is not None and self.kind(p) == want:
                    return p
        return None

    def image(self, info: dict) -> str:
        # the docker pass's node for the same reference
        return self.add({"id": f"extimage:{info['name']}", "label": info["name"], "type": "image",
                         "file_type": "docker", "source_file": self.rel,
                         "repository": info["repository"], "tag": info.get("tag")})

    # ---- one file ---------------------------------------------------------------------------
    def run_file(self, path: Path, rel: str):
        text = self.read(path)
        if text is None or "\x00" in text or ("pipeline" not in text and "node" not in text):
            return
        self.begin(rel, len(text))
        try:
            tree = _tree(_tokens(text))
            style = _style(tree)
            if style is None:
                return
            self.add({"id": self.pid, "label": rel, "type": "jenkins_pipeline", "file_type": _FT,
                      "source_file": rel, "style": style})
            ctx = _Ctx(owner=self.pid, parent=self.pid, path="", seq=[None], conf="INFERRED",
                       base="", post=None, par=False)
            self.walk(tree, ctx)
        except _Full:
            pass
        except Exception:
            # a shape this pass did not foresee costs this file, never the build
            self.rollback()

    # ---- scripted code (and declarative `steps`) ---------------------------------------------
    def walk(self, block, ctx: _Ctx):
        for stmt in block:
            ctx = self.statement(stmt, ctx)

    def statement(self, stmt, ctx: _Ctx) -> _Ctx:
        self.charge(len(stmt) + 1)
        self.scan(stmt, ctx)
        name = _head(stmt)
        blks = [t[1] for t in stmt if t[0] == "blk"]
        dotted = _is(_at(stmt, 1), "op", ".")
        if name == "pipeline" and blks and len(stmt) == 2:
            self.declarative(blks[0], ctx)
            return ctx
        if name == "stage" and not dotted:
            sid, path = self.stage(_stage_name(stmt), ctx, stmt[0][2])
            if not blks:
                return ctx.but(owner=sid)      # legacy `stage 'x'`: the steps after it are its own
            body = ctx.but(owner=sid, parent=sid, path=path, seq=[None], conf="INFERRED",
                           par=False)
            for b in blks:
                self.walk(b, body)
            return ctx
        if name == "parallel" and not dotted:
            for b in blks:
                self.walk(b, ctx.but(seq=[None], par=True))
            return ctx
        if name == "dir" and blks and not dotted:
            sub = ctx.but(base=self.subdir(ctx.base, _first_str(stmt)))
            for b in blks:
                self.walk(b, sub)
            return ctx
        if name == "properties" and not dotted:
            self.properties(stmt)
        # a method body, or a closure stored for later, does not run in this sequence
        fresh = name in _METHOD_WORDS or any(_is(t, "op", "=") for t in stmt)
        for b in blks:
            self.walk(b, ctx.but(seq=[None]) if fresh else ctx)
        return ctx

    def stage(self, sname: str, ctx: _Ctx, line: int) -> tuple[str, str]:
        path = f"{ctx.path}/{sname}" if ctx.path else sname
        sid = self.add({"id": f"jenkinsstage:{self.rel}#{path}", "label": sname,
                        "type": "jenkins_stage", "file_type": _FT, "source_file": self.rel,
                        "source_location": f"L{line}", "parallel": True if ctx.par else None})
        self.edge(ctx.parent, sid, "contains")
        if ctx.seq is not None:
            if ctx.seq[0] is not None:
                self.edge(sid, ctx.seq[0], "depends_on", ctx.conf)
            ctx.seq[0] = sid
        return sid, path

    def properties(self, stmt):
        for j, t in enumerate(stmt):
            if t[0] != "id" or not _is(_at(stmt, j + 1), "op", "("):
                continue
            if t[1] == "parameters":
                self.names(self.pid, "parameters", _name_args(stmt[j + 2:self.close(stmt, j + 1)]))
            elif t[1] == "pipelineTriggers":
                c = self.close(stmt, j + 1)
                kinds = [stmt[k][1] for k in range(j + 2, min(c, len(stmt)))
                         if stmt[k][0] == "id" and _is(_at(stmt, k + 1), "op", "(")
                         and not _is(_at(stmt, k - 1), "op", ".") and _NAME.fullmatch(stmt[k][1])]
                self.names(self.pid, "triggers", kinds)

    # ---- declarative ------------------------------------------------------------------------
    def declarative(self, block, ctx: _Ctx):
        for stmt in block:
            self.charge(len(stmt) + 1)
            name, blk = _head(stmt), _blk(stmt)
            if name == "agent":
                self.agent(stmt, ctx)
            elif blk is None:
                continue
            elif name == "environment":
                self.environment(blk, ctx)
            elif name == "parameters":
                self.names(ctx.owner, "parameters", _name_args([t for s in blk for t in s]))
            elif name == "triggers":
                self.names(ctx.owner, "triggers",
                           [h for h in (_head(s) for s in blk) if h and _NAME.fullmatch(h)])
            elif name == "tools":
                self.tools(blk, ctx)
            elif name == "stages":
                self.stages(blk, ctx.but(seq=[None], conf="EXTRACTED"))
            elif name == "post":
                self.post(blk, ctx)

    def stages(self, block, ctx: _Ctx):
        for stmt in block:
            self.charge(len(stmt) + 1)
            if _head(stmt) != "stage":
                continue
            sid, path = self.stage(_stage_name(stmt), ctx, stmt[0][2])
            blk = _blk(stmt)
            if blk is not None:
                self.stage_body(blk, ctx.but(owner=sid, parent=sid, path=path, base="",
                                             post=None, par=False))

    def stage_body(self, block, ctx: _Ctx):
        for stmt in block:
            self.charge(len(stmt) + 1)
            name, blk = _head(stmt), _blk(stmt)
            if name == "agent":
                self.agent(stmt, ctx)
            elif blk is None:
                continue
            elif name == "environment":
                self.environment(blk, ctx)
            elif name == "tools":
                self.tools(blk, ctx)
            elif name == "steps":
                self.walk(blk, ctx.but(seq=[None], conf="INFERRED"))
            elif name == "stages":
                self.stages(blk, ctx.but(seq=[None], conf="EXTRACTED"))
            elif name == "parallel":
                self.stages(blk, ctx.but(seq=None, par=True))
            elif name == "matrix":
                self.nodes[ctx.owner]["matrix"] = True
                for s in blk:
                    if _head(s) == "agent":
                        self.agent(s, ctx)
                    elif _head(s) == "stages" and _blk(s) is not None:
                        self.stages(_blk(s), ctx.but(seq=[None], conf="EXTRACTED"))
            elif name == "post":
                self.post(blk, ctx)

    def post(self, block, ctx: _Ctx):
        conds = []
        for stmt in block:
            c, blk = _head(stmt), _blk(stmt)
            if c and blk is not None and _NAME.fullmatch(c):
                conds.append(c)
                self.walk(blk, ctx.but(seq=[None], conf="INFERRED", post=c))
        self.names(ctx.owner, "post", conds)

    def environment(self, block, ctx: _Ctx):
        names = []
        for stmt in block:
            self.charge(len(stmt) + 1)
            if (len(stmt) >= 2 and stmt[0][0] == "id" and _is(stmt[1], "op", "=")
                    and _NAME.fullmatch(stmt[0][1])):
                names.append(stmt[0][1])
                # the value is scanned only for credentials('<id>') and steps, never kept
                self.scan(stmt[2:], ctx, variable=stmt[0][1])
        self.names(ctx.owner, "environment", names)

    def tools(self, block, ctx: _Ctx):
        out = []
        for stmt in block:
            h = _head(stmt)
            if not h or not _NAME.fullmatch(h):
                continue
            s = _first_str(stmt)
            out.append(f"{h}:{s}" if isinstance(s, str) and _PLAIN.fullmatch(s) else h)
        self.names(ctx.owner, "tools", out)

    def agent(self, stmt, ctx: _Ctx):
        kind = None
        nxt = _at(stmt, 1)
        if _is(nxt, "id"):
            kind = nxt[1]
        blk = _blk(stmt)
        for s in blk or []:
            h = _head(s)
            if not h:
                continue
            kind = kind or h
            if h == "docker":
                self.agent_docker(s, ctx)
            elif h == "dockerfile":
                self.agent_dockerfile(s, ctx)
        if kind and _NAME.fullmatch(kind) and ctx.owner in self.nodes:
            self.nodes[ctx.owner]["agent"] = kind

    def agent_docker(self, s, ctx: _Ctx):
        img = s[1][1] if _is(_at(s, 1), "str") else None
        for t in _blk(s) or []:
            if _head(t) == "image":
                img = _first_str(t) or img
            self.scan(t, ctx)               # registryCredentialsId; registryUrl/args are dropped
        info = _image(img)
        if info:
            self.step_edge(ctx, self.image(info), "uses_image", agent=True,
                           digest=info.get("digest"), tag_unresolved=info.get("tag_unresolved"))

    def agent_dockerfile(self, s, ctx: _Ctx):
        fname, d = "Dockerfile", ""
        blk = _blk(s)
        if blk is None and not _is(_at(s, 1), "id", "true"):
            return
        for t in blk or []:
            h = _head(t)
            if h == "filename":
                fname = _first_str(t) or fname
            elif h == "dir":
                d = _first_str(t) or ""
            self.scan(t, ctx)
        base = _join("", d) if d else ""
        if base is None:
            return
        direct = _join(base, fname)
        if direct is not None and self.kind(direct) == "file":
            rel, conf = direct, "EXTRACTED"
        else:
            rel, conf = self.resolve(base, fname, "file"), "INFERRED"
        if rel:
            self.step_edge(ctx, self.add(_dockerfile_node(rel)), "builds", conf, agent=True)

    # ---- steps inside a statement -----------------------------------------------------------
    def scan(self, toks, ctx: _Ctx, variable=None):
        calls: list = []                    # enclosing call names, for `via`
        n = len(toks)
        head = _head(toks)
        for j in range(n):
            k, v, line = toks[j]
            if k == "op":
                if v in ("(", "["):
                    p = toks[j - 1] if j else None
                    calls.append(p[1] if p is not None and p[0] == "id" else None)
                elif v in (")", "]") and calls:
                    calls.pop()
                continue
            if k != "id":
                continue
            prev = toks[j - 1] if j else None
            dotted = prev is not None and prev[0] == "op" and prev[1] in (".", "?.", "*.")
            nxt = toks[j + 1] if j + 1 < n else None
            if dotted:
                owner_tok = _at(toks, j - 2)
                if (v == "push" and _is(nxt, "op", "(") and _is(owner_tok, "id")
                        and owner_tok[1] in self.vars):
                    self.push_var(self.vars[owner_tok[1]], toks, j + 1, ctx)
                    continue
                if not (v in _SHELL_STEPS and _is(owner_tok, "id")
                        and owner_tok[1] in ("steps", "script", "this")):
                    continue
            if v in _SHELL_STEPS:
                body = self.step_body(toks, j)
                if body is not None:
                    self.shell(body, _SHELL_STEPS[v], ctx, line)
            elif v in ("credentialsId", "registryCredentialsId", "credentials") and _is(nxt, "op", ":"):
                via = next((c for c in reversed(calls) if c), None) or head or v
                for s in self.str_or_list(toks, j + 2):
                    self.credential(s, ctx, via, variable)
            elif v in ("credentials", "registryCredentialsId") and _is(nxt, "op", "("):
                c = self.close(toks, j + 1)
                strs = _positional(toks[j + 2:c])
                if strs:
                    self.credential(strs[0], ctx, v, variable)
            elif v == "registryCredentialsId" and _is(nxt, "str"):
                self.credential(nxt[1], ctx, v, variable)
            elif v == "sshagent" and _is(nxt, "op", "("):
                for s in self.str_or_list(toks, j + 2):
                    self.credential(s, ctx, v, variable)
            elif (v == "docker" and _is(nxt, "op", ".") and _is(_at(toks, j + 2), "id")
                    and _is(_at(toks, j + 3), "op", "(")):
                self.docker_api(toks, j, ctx)
            elif v == "load" and (_is(nxt, "str") or _is(nxt, "op", "(")):
                s = nxt[1] if nxt[0] == "str" else next(iter(_positional(toks[j + 2:self.close(toks, j + 1)])), None)
                rel = self.resolve(ctx.base, s, "file")
                if rel:
                    self.step_edge(ctx, self.add(file_node(rel)), "runs_script", "INFERRED",
                                   step="load")

    def str_or_list(self, toks, i) -> list[str]:
        t = _at(toks, i)
        if _is(t, "str"):
            return [t[1]]
        if _is(t, "op", "["):
            c = self.close(toks, i)
            return _positional(toks[i + 1:c])
        return []

    def credential(self, s, ctx: _Ctx, via, variable):
        if not isinstance(s, str) or not _CRED_ID.fullmatch(s):
            return
        cid = self.add({"id": f"jenkinscredential:{s}", "label": s, "type": "jenkins_credential",
                        "file_type": _FT, "source_file": self.rel})
        self.step_edge(ctx, cid, "uses_credential", via=via, variable=variable)

    def step_body(self, toks, j):
        """The script of `sh '...'`, `sh(script: '...')`, `bat label: '', script: '...'`."""
        nxt = _at(toks, j + 1)
        if _is(nxt, "str"):
            return nxt[1]
        if _is(nxt, "op", "("):
            c = self.close(toks, j + 1)
            args = toks[j + 2:c]
            v = _named(args, "script")
            if _is(v, "str"):
                return v[1]
            return args[0][1] if args and args[0][0] == "str" else None
        if _is(nxt, "id") and _is(_at(toks, j + 2), "op", ":"):
            rest = toks[j + 1:j + 1 + _MAX_SCAN]
            self.charge(len(rest))
            v = _named(rest, "script")
            return v[1] if _is(v, "str") else None
        return None

    def docker_api(self, toks, j, ctx: _Ctx):
        meth = toks[j + 2][1]
        o = j + 3
        c = self.close(toks, o)
        strs = _positional(toks[o + 1:c])
        var = toks[j - 2][1] if (j >= 2 and _is(toks[j - 1], "op", "=")
                                 and _is(toks[j - 2], "id")) else None
        if meth == "withRegistry":
            if len(strs) >= 2:                  # (url, credentialsId): the URL is never kept
                self.credential(strs[1], ctx, "withRegistry", None)
        elif meth == "image":
            info = _image(strs[0]) if strs else None
            if not info:
                return
            if var:
                self.vars[var] = info
            if _is(_at(toks, c + 1), "op", ".") and _is(_at(toks, c + 2), "id", "push") \
                    and _is(_at(toks, c + 3), "op", "("):
                self.push_var(info, toks, c + 3, ctx)
            else:
                self.step_edge(ctx, self.image(info), "uses_image",
                               tag_unresolved=info.get("tag_unresolved"))
        elif meth == "build":
            info = _image(strs[0]) if strs else None
            if info and var:
                self.vars[var] = info
            words = ["."]
            if len(strs) >= 2:
                cmds = shell_commands(strs[1][:_MAX_SCRIPT])
                words = cmds[0] if cmds else ["."]
            self.docker_cli(["build"] + words, ctx.base, ctx, "docker.build")

    def push_var(self, info: dict, toks, o, ctx: _Ctx):
        """`<image>.push()` / `.push('<tag>')`: the reference with that tag."""
        c = self.close(toks, o)
        tags = _positional(toks[o + 1:c])
        if tags:
            tagged = _image(f"{info['repository']}:{tags[0]}")
            info = tagged or info
        self.step_edge(ctx, self.image(info), "pushes", tag_unresolved=info.get("tag_unresolved"))

    # ---- shell text -------------------------------------------------------------------------
    def shell(self, text: str, step: str, ctx: _Ctx, line: int):
        text = text[:_MAX_SCRIPT]
        self.charge(len(text))
        try:
            cmds = shell_commands(text, powershell=step != "sh")
        except Exception:
            return
        cwd = ctx.base
        for words in cmds:
            self.charge(len(words) + 1)
            words = list(words)
            while words and (_ASSIGN.match(words[0]) or words[0].lower() in _PREFIX_WORDS
                             or words[0].startswith("-")):
                words.pop(0)
            if not words:
                continue
            w0 = words[0].lstrip("@")
            prog = _prog(w0)
            rest = words[1:]
            if prog in ("cd", "pushd", "set-location", "sl", "chdir"):
                target = next((w for w in rest if not w.startswith("-")
                               and not (step == "bat" and w.lower() == "/d")), None)
                cwd = self.subdir(cwd, target)
            elif prog in _DOCKER_BINS:
                self.docker_cli(rest, cwd, ctx, step)
            elif prog in ("helm", "helm3"):
                self.helm(rest, cwd, ctx, line)
            elif prog == "dotnet":
                self.dotnet(rest, cwd, ctx, line)
            elif prog == "eb":
                self.eb(rest, ctx, line)
            elif prog == "aws":
                self.aws(rest, ctx, line)
            else:
                self.run_local(prog, w0, rest, cwd, ctx, step)

    def run_local(self, prog, w0, rest, cwd, ctx: _Ctx, step):
        cand = None
        if prog in _SHELL_INTERP or _PYTHON.fullmatch(prog):
            args = [w for w in rest if not w.startswith("-")]
            cand = args[0] if args else None
        elif prog in ("pwsh", "powershell"):
            for k, w in enumerate(rest):
                if w.lower() in ("-file", "-f") and k + 1 < len(rest):
                    cand = rest[k + 1]
                    break
            else:
                cand = next((w for w in rest if not w.startswith("-")
                             and w.lower().endswith(".ps1")), None)
        elif prog == "cmd":
            for k, w in enumerate(rest):
                if w.lower() in ("/c", "/k") and k + 1 < len(rest):
                    cand = rest[k + 1]
                    break
        elif prog in ("call", "start"):
            cand = rest[0] if rest else None
        elif ("/" in w0 or "\\" in w0
              or (step != "sh" and posixpath.splitext(w0)[1].lower() in (".bat", ".cmd", ".ps1"))):
            cand = w0
        rel = self.resolve(cwd, cand, "file") if cand else None
        if rel:
            self.step_edge(ctx, self.add(file_node(rel)), "runs_script", "INFERRED", step=step)

    def docker_cli(self, args, cwd, ctx: _Ctx, step):
        args = list(args)
        while args and args[0].startswith("-"):
            flag = args.pop(0)
            if flag in _DOCKER_GLOBAL_VALUE and args:
                args.pop(0)
        sub = []
        while args and len(sub) < 2 and args[0] in ("build", "buildx", "image", "push"):
            sub.append(args.pop(0))
            if sub[-1] in ("build", "push"):
                break
        if not sub or sub[-1] not in ("build", "push"):
            return
        value_flags = _DOCKER_BUILD_VALUE if sub[-1] == "build" else _DOCKER_PUSH_VALUE
        opts: dict[str, list[str]] = {}
        pos: list[str] = []
        while args:
            w = args.pop(0)
            if w.startswith("-") and w != "-":
                k, eq, v = w.partition("=")
                if not eq and k in value_flags and args:
                    v = args.pop(0)
                opts.setdefault(k, []).append(v)
            else:
                pos.append(w)
        if sub[-1] == "push":
            info = _image(pos[0]) if pos else None
            if info:
                self.step_edge(ctx, self.image(info), "pushes",
                               tag_unresolved=info.get("tag_unresolved"))
            return
        ctx_arg = pos[0] if pos else None
        fflag = (opts.get("-f") or opts.get("--file") or [None])[-1]
        if ctx_arg in (None, "-") or fflag == "-":
            return
        bctx = self.resolve(cwd, ctx_arg, "dir")
        if bctx is None:
            return
        df = self.resolve(cwd, fflag, "file") if fflag else (
            p if (p := _join(bctx, "Dockerfile")) is not None and self.kind(p) == "file" else None)
        if df:
            tgt = (opts.get("--target") or [None])[-1]
            self.step_edge(ctx, self.add(_dockerfile_node(df)), "builds", "INFERRED",
                           stage=tgt if tgt and _NAME.fullmatch(tgt) else None,
                           step=step if step == "docker.build" else None)
        if "--push" in opts:
            for t in opts.get("-t", []) + opts.get("--tag", []):
                info = _image(t)
                if info:
                    self.step_edge(ctx, self.image(info), "pushes",
                                   tag_unresolved=info.get("tag_unresolved"))

    def helm(self, args, cwd, ctx: _Ctx, line):
        call = _parse_helm(list(args))
        if not call:
            return
        rname, ns, chart = call
        if not _K8S_NAME.fullmatch(rname) or (ns is not None and not _K8S_NAME.fullmatch(ns)):
            return
        # the id and attributes the k8s pass gives a release, so the two join
        rid = self.add({"id": f"helmrelease:{ns or '-'}/{rname}", "label": rname,
                        "type": "helm_release", "file_type": "helm", "namespace": ns or None,
                        "source_file": self.rel, "source_location": f"L{line}"})
        self.step_edge(ctx, rid, "deploys")
        local = self.resolve(cwd, chart, "dir")
        if local is not None and self.kind(posixpath.join(local, "Chart.yaml")) == "file":
            # the k8s pass graphs every chart directory under this id ("." for the root)
            self.edge(rid, f"helmchart:{local or '.'}", "installs", "INFERRED")

    def project(self, d, target) -> str | None:
        """The project/solution `dotnet` acts on: `target` (a file, or a directory holding one),
        else the single one in directory `d`."""
        if target:
            p = self.resolve(d, target, "file")
            if p is not None:
                m = _manifest_node(p)
                return p if m and m["type"] in ("dotnet_project", "solution") else None
            d = self.resolve(d, target, "dir")
        if d is None or self.kind(d) != "dir" or not self.probe():
            return None
        try:
            with os.scandir(self.root_res / d if d else self.root_res) as it:
                names = sorted(e.name for e in it if e.is_file())
        except OSError:
            return None
        cands = [x for x in names if x.lower().endswith(_PROJECT_EXT + _SOLUTION_EXT)]
        if len(cands) != 1:
            return None
        rel = f"{d}/{cands[0]}" if d else cands[0]
        return rel if self.kind(rel) == "file" else None

    def dotnet(self, args, cwd, ctx: _Ctx, line):
        if not args:
            return
        sub = args[0].lower()
        if sub in _DOTNET_BUILDS:
            target, i = None, 1
            while i < len(args):
                a = args[i]
                if a == "--":
                    break                   # `dotnet test -- <RunSettings>`: never a project
                if a.startswith("-"):
                    if "=" not in a and ":" not in a and a in _DOTNET_VALUE:
                        i += 1
                elif target is None:
                    target = a
                i += 1
            proj = self.project(cwd, target)
            if proj:
                self.step_edge(ctx, self.add(_manifest_node(proj)), _DOTNET_BUILDS[sub],
                               "INFERRED", command=f"dotnet {sub}")
        elif sub == "eb" and len(args) > 1 and args[1].lower() == "deploy-environment":
            opts = _opts(args[2:])
            app = opts.get("-app") or opts.get("--application") or opts.get("--application-name")
            env = opts.get("-env") or opts.get("--environment") or opts.get("--environment-name")
            loc = cwd
            pl = opts.get("-pl") or opts.get("--project-location")
            if pl:
                loc = self.subdir(cwd, pl)
            proj = self.project(loc, None)
            if proj:
                self.step_edge(ctx, self.add(_manifest_node(proj)), "builds", "INFERRED",
                               command="dotnet eb deploy-environment")
            conf = "EXTRACTED"
            if not (app and env):
                cfg = opts.get("-cfg") or opts.get("--config-file") or "aws-beanstalk-tools-defaults.json"
                defaults = self.beanstalk_defaults(loc, cfg)
                if defaults:
                    app = app or defaults.get("application")
                    env = env or defaults.get("environment")
                    conf = "INFERRED"
            self.beanstalk(app, env, ctx, line, conf, "dotnet-eb")

    def beanstalk_defaults(self, loc, cfg) -> dict | None:
        """The application and environment NAMES of a dotnet eb defaults file; no other key."""
        rel = self.resolve(loc, cfg, "file")
        if rel is None:
            return None
        text = self.read(self.root / rel)
        if text is None:
            return None
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            return None
        if not isinstance(data, dict):
            return None
        return {k: data[k] for k in ("application", "environment") if isinstance(data.get(k), str)}

    def beanstalk(self, app, env, ctx: _Ctx, line, conf, tool):
        # the Beanstalk pass's name rules and node shapes, so both passes give one deployment
        # one id, and an environment only CI names still hangs off its application
        env = _eb_env(env)
        if env is None:
            return
        app = _eb_app(app)
        eid = self.add({**_eb_env_node(app, env, self.rel), "source_location": f"L{line}"})
        self.step_edge(ctx, eid, "deploys", conf, tool=tool)
        if app:
            self.edge(self.add(_eb_app_node(app, self.rel)), eid, "has_environment", conf)

    def eb(self, args, ctx: _Ctx, line):
        if not args or args[0] != "deploy":
            return
        env, i = None, 1
        while i < len(args):
            a = args[i]
            if a.startswith("-"):
                if "=" not in a and a in _EB_VALUE:
                    i += 1
            elif env is None:
                env = a
            i += 1
        self.beanstalk(None, env, ctx, line, "EXTRACTED", "eb")

    def aws(self, args, ctx: _Ctx, line):
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 2 if ("=" not in args[i] and args[i] in _AWS_GLOBAL_VALUE) else 1
        if i + 1 >= len(args):
            return
        svc, sub = args[i].lower(), args[i + 1].lower()
        opts = _opts(args[i + 2:])
        if svc == "elasticbeanstalk":
            app = _eb_app(opts.get("--application-name"))
            if sub == "create-application-version":
                if app:
                    self.apps[ctx.owner] = app
            elif sub in ("update-environment", "create-environment"):
                conf = "EXTRACTED" if app else "INFERRED"
                self.beanstalk(app or self.apps.get(ctx.owner), opts.get("--environment-name"),
                               ctx, line, conf, "aws-cli")
        elif svc == "ecr" and sub == "put-image":
            repo, tag = opts.get("--repository-name"), opts.get("--image-tag")
            info = _image(f"{repo}:{tag}" if repo and tag else repo)
            if info:
                self.step_edge(ctx, self.image(info), "pushes",
                               tag_unresolved=info.get("tag_unresolved"), tool="aws-ecr")


def extract(root: Path, exclude: frozenset[str] = frozenset(),
            include: frozenset[str] = frozenset()) -> dict:
    root = Path(root)
    p = _Pass(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not skip_dir(d, exclude, include))
        d = Path(dirpath)
        drel = "" if d == root else d.relative_to(root).as_posix()
        for fn in sorted(filenames):
            if is_pipeline_name(fn):
                p.run_file(d / fn, f"{drel}/{fn}" if drel else fn)
    return {"nodes": list(p.nodes.values()), "edges": p.edges}
