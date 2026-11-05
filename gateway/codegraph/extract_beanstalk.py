"""AWS Elastic Beanstalk extractor - what an application bundle configures, runs and deploys to.

Beanstalk configuration lives in hidden directories (`.ebextensions`, `.platform`,
`.elasticbeanstalk`) that the shared prune rule (`skip_dir`) drops from every walk, and in a few
fixed file names no other pass owns. This repo-level pass (stdlib + PyYAML, no tree-sitter, no
AWS call) walks the tree with the shared prune rule - plus bin/ and obj/ next to a .NET project,
build output that carries copies of the bundle - and, in every directory it visits, reads:

  .ebextensions/*.config          YAML or JSON (CloudFormation `!Ref`-style tags are tolerated):
                                  option_settings (namespace and option NAMES), packages
                                  (manager + package names), commands / container_commands
                                  (names, plus the local scripts they invoke), files (target
                                  paths), services (manager + service names), Resources
                                  (logical ids, types, DependsOn)
  .elasticbeanstalk/config.yml    EB CLI: global.application_name, environment names from
                                  branch-defaults and environment-defaults, default_platform,
                                  default_region
  .platform/hooks/<phase>/*       platform hooks (prebuild, predeploy, postdeploy) - files directly
  .platform/confighooks/<phase>/* in a phase directory, which the platform runs; never read
  Dockerrun.aws.json              v1 Image/Ports, v2 containerDefinitions (name, image, ports,
                                  links, environment NAMES)
  Procfile                        process names, plus the local script a process runs - only in a
                                  directory that is a bundle by other evidence (Heroku uses the
                                  same file name)
  aws-<tool>-tools-defaults*.json AWS .NET deployment-tool defaults (`dotnet eb`, `dotnet lambda`,
                                  ...): application, environment, solution-stack, region, project

The directory holding any of these is the source bundle: container_commands run there, Procfile
commands and hook paths resolve against it, and the EB CLI / `dotnet eb` deploy it.

Emitted nodes:
  beanstalkbundle:<dir>                  a source bundle (`.` = repository root)
  beanstalkconfig:<rel>                  an .ebextensions config file - attrs: namespaces, options
                                         (`<namespace>/<option>`), environment_keys (names under
                                         aws:elasticbeanstalk:application:environment), packages
                                         (`<manager>:<name>`), commands, container_commands,
                                         files (target paths, shape only), services
  beanstalkresource:<rel>#<LogicalId>    a CloudFormation resource a config adds - resource_type
  beanstalkcli:<rel>                     an EB CLI config.yml - application, platform, region
  awsdeploydefaults:<rel>                a deployment-tool defaults file - tool, option_keys (key
                                         NAMES), region, solution_stack
  beanstalkapp:<application>             an application, by name
  beanstalkenv:<application or ->/<env>  an environment; the same id the Jenkins pass gives
                                         `dotnet eb deploy-environment -app A -env E`, `-` when no
                                         application is named
  dockerrun:<rel>                        a Dockerrun.aws.json - version, ports
  dockerruncontainer:<rel>#<name>        a v2 container definition - ports, environment_keys,
                                         essential
  beanstalkprocess:<procfile rel>#<name> a Procfile process
  extimage:<name[:tag]>                  an image, in the node shape the docker pass writes
  module:<rel> / file:<rel> / project:<rel>
                                         local scripts and .NET projects, in the node shape the
                                         docker / tree-sitter / manifests passes write, so the
                                         graphs join and the target is typed under [graph-slim]

Emitted edges: contains (bundle -> config / cli / dockerrun / defaults), runs (bundle -> hook
script; hook and phase attrs), runs_script (INFERRED: config or process -> a local script a
command invokes; section and command attrs), defines_resource (config -> resource), depends_on
(resource -> resource), has_environment (app -> env), targets (cli / defaults -> env; branches
attr), deploys_to (bundle -> env, EXTRACTED from the EB CLI config, INFERRED from a defaults
file; project -> env, INFERRED), configures (defaults -> project), has_container (dockerrun ->
container), uses_image (dockerrun or container -> extimage), links (container -> container),
has_process (bundle -> process).

Deliberately absent - this is a hard rule, not an omission: no VALUES. No option_settings values
(environment property values, connection strings, instance profiles, roles, key pairs, VPC or
subnet ids), no command text, `env`, `cwd` or `test` values, no file content or source URL, no
package versions or URLs, no Resource Properties or Outputs, no Dockerrun Authentication
(bucket, key), volumes, environment values, entrypoints or commands, no Procfile command text,
no defaults-file values other than application, environment, solution stack, project path and a
plain AWS region code (no profile, instance profile, service role, key pair, bucket, VPC,
subnets, credentials). An option name that is a URL path (static-file mappings) keeps only its
shape, by the rule extract_appsmith uses: plain word segments or `v<n>` stay, every other
segment becomes `{}`. File target paths keep plain segments only, and any name carrying a long
letter-and-digit run (a token shape) is dropped. An image reference with userinfo or a scheme is
not graphed. Hook script contents are not read; nothing is evaluated, fetched or executed.

A file that is malformed, oversized, unreadable or a symlink leaving the repository is skipped;
one whose shape still trips the pass costs that file's own nodes and edges, never the build.
Walks are sorted; each file has node and edge caps and a work share proportional to its size,
and an option map an alias repeats is read once, so YAML aliases that repeat the same command
or option map cannot multiply the work. app_name / env_name / env_node / app_node are the name
rules and node shapes the Jenkins pass reuses for the same environments.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
from pathlib import Path

import yaml

from .extract_ansible import skip_dir
from .extract_docker import file_node, parse_image, shell_commands
from .extract_manifests import manifest_kind, node_for as _manifest_node

_FT = "beanstalk"
_MAX_BYTES = 2 * 1024 * 1024    # a larger config is generated or hostile: skip it
_MAX_NODES = 2000               # new nodes one file may add
_MAX_EDGES = 4000               # edges one file may add
_MAX_NAMES = 2000               # names one attribute may list
_MAX_HOOKS = 500                # hook files linked per phase directory
_WORK_PER_BYTE = 8              # work one file may spend (entries walked, command text scanned)
_WORK_BASE = 50_000             # ... is this plus _WORK_PER_BYTE x its size
_EB_DIRS = (".ebextensions", ".elasticbeanstalk", ".platform")
_DOTNET_OUTPUT = frozenset({"bin", "obj"})
_HOOK_KINDS = ("hooks", "confighooks")
_HOOK_PHASES = ("prebuild", "predeploy", "postdeploy")
_ENV_NS = "aws:elasticbeanstalk:application:environment"
_STAGING = ("/var/app/staging/", "/var/app/current/", "/var/app/ondeck/")

_DEFAULTS = re.compile(r"aws-([a-z0-9]{1,32})-tools-defaults[A-Za-z0-9_.-]{0,64}\.json", re.I)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}")           # env var / key / option name
_OPTION = re.compile(r"[A-Za-z_][A-Za-z0-9_. :-]{0,127}")       # EB option names have spaces
_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_CMD_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_PKG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}")
_MANAGER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
_LOGICAL_ID = re.compile(r"[A-Za-z0-9]{1,255}")
_RES_TYPE = re.compile(r"[A-Za-z0-9]{1,32}(?:::[A-Za-z0-9_.@-]{1,64}){1,3}")
_APP = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}")
_ENV = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,39}")
_PROC = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,62}")
_CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
_PLATFORM = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()+/-]{0,159}")
_REGION = re.compile(r"[a-z]{2}(?:-gov|-iso[a-z]?)?-[a-z]{2,12}-\d{1,2}")
_BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,127}")
_TOKENISH = re.compile(r"[A-Za-z0-9+=]{24,}")
_PLAIN_SEGMENT = re.compile(r"[A-Za-z][A-Za-z_-]{0,23}|v\d{1,2}")   # extract_appsmith's rule
_FS_SEGMENT = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.+-]{0,63}")
_DIGEST = re.compile(r"[A-Za-z0-9_+.-]{1,32}:[A-Fa-f0-9]{32,128}")
_PROCFILE_LINE = re.compile(r"([A-Za-z0-9_-]{1,63})[ \t]*:(.*)")
_PY = re.compile(r"(python|pypy)(\d+(\.\d+)?)?")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}=")
_UNSAFE_PATH = ("$", "%", "*", "?", "[", "~", "`", "{", "://")

_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "source", "."}
_PWSH = {"powershell", "pwsh"}
_INTERPRETERS = {"node", "ruby", "perl", "php"}
_PREFIX = {"exec", "nohup", "time", "command", "nice", "then", "do", "!"}
_PWSH_VALUE = {"-executionpolicy", "-ep", "-workingdirectory", "-wd", "-outputformat", "-of",
               "-inputformat", "-if", "-configurationname", "-settingsfile", "-windowstyle"}


class _Loader(yaml.SafeLoader):
    """SafeLoader that maps every local tag (`!Ref`, `!GetAtt`, `!Sub`...) to None: the payload
    is never wanted, and without this the whole config would fail to load. The pure-Python
    SafeLoader on purpose: it raises RecursionError on deep nesting where CSafeLoader crashes."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


# --------------------------------------------------------------------------------------------
# value safety: names and shapes only

def _tokenish(s: str) -> bool:
    """A long run of letters and digits with no separator: the shape of a key or token."""
    for m in _TOKENISH.finditer(s):
        run = m.group(0)
        if any(c.isdigit() for c in run) and any(c.isalpha() for c in run):
            return True
    return False


def _name(v, pattern: re.Pattern) -> str | None:
    if not isinstance(v, str):
        return None
    v = v.strip()
    if not v or not pattern.fullmatch(v) or _tokenish(v):
        return None
    return v


def app_name(v) -> str | None:
    """An Elastic Beanstalk application name (spaces allowed), or None. Shared with the Jenkins
    pass, so both read one deployment as one id."""
    return _name(v, _APP)


def env_name(v) -> str | None:
    """An Elastic Beanstalk environment name (letters, digits, `-`), or None."""
    return _name(v, _ENV)


def env_node(app: str | None, env: str, src: str) -> dict:
    """The beanstalkenv: node for validated names (see app_name / env_name); `-` stands for an
    application nobody named."""
    return {"id": f"beanstalkenv:{app or '-'}/{env}", "label": env, "type": "beanstalk_environment",
            "file_type": _FT, "source_file": src, "application": app}


def app_node(app: str, src: str) -> dict:
    return {"id": f"beanstalkapp:{app}", "label": app, "type": "beanstalk_application",
            "file_type": _FT, "source_file": src}


def _url_path_shape(p: str) -> str | None:
    """The shape of a URL path, by extract_appsmith's rule: query and fragment dropped, plain word
    segments (or `v<n>`) kept, every other segment `{}`."""
    for sep in ("?", "#", ";"):
        p = p.split(sep, 1)[0]
    segs = []
    for seg in p.strip().split("/"):
        seg = seg.strip()
        segs.append(seg if seg == "" or _PLAIN_SEGMENT.fullmatch(seg) else "{}")
    out = "/".join(segs)[:256]
    return out or None


def _fs_path_shape(p) -> str | None:
    """A filesystem target path with only plain segments kept (`{}` for the rest)."""
    if not isinstance(p, str):
        return None
    p = p.strip().replace("\\", "/")
    if not p or len(p) > 512 or "://" in p:
        return None
    prefix = ""
    if len(p) >= 2 and p[0].isalpha() and p[1] == ":" and (len(p) == 2 or p[2] == "/"):
        prefix, p = p[:2].lower(), p[2:]
    segs = []
    for seg in p.split("/"):
        if seg == "" or (_FS_SEGMENT.fullmatch(seg) and not _tokenish(seg)):
            segs.append(seg)
        else:
            segs.append("{}")
    return prefix + "/".join(segs)


def _option_name(v) -> str | None:
    if not isinstance(v, str):
        return None
    v = v.strip()
    if v.startswith("/"):
        return _url_path_shape(v)
    return _name(v, _OPTION)


def _platform(v) -> str | None:
    """A platform / solution stack name. A platform ARN keeps only its `platform/...` tail."""
    if not isinstance(v, str):
        return None
    v = v.strip()
    if v.lower().startswith("arn:"):
        cut = v.find(":platform/")
        if cut < 0:
            return None
        v = v[cut + len(":platform/"):]
    return _name(v, _PLATFORM)


def _region(v) -> str | None:
    return v.strip() if isinstance(v, str) and _REGION.fullmatch(v.strip()) else None


def _port(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, str) and v.strip().isdigit() and len(v.strip()) <= 5:
        v = int(v.strip())
    return v if isinstance(v, int) and 0 < v < 65536 else None


def _image(ref) -> dict | None:
    """parse_image, refusing anything that could carry credentials: a scheme, or an `@` that is
    not followed by a plain digest (`user:pw@host/img` would otherwise yield tag `pw`)."""
    if not isinstance(ref, str) or "://" in ref:
        return None
    ref = ref.strip()
    if "@" in ref:
        before, _, after = ref.partition("@")
        if "@" in after or not _DIGEST.fullmatch(after) or not before:
            return None
    info = parse_image(ref)
    if info is None or _tokenish(info["repository"].rsplit("/", 1)[-1]):
        return None
    return info


# --------------------------------------------------------------------------------------------
# local scripts a command line runs

def _strip_prefix(words: list[str]) -> list[str]:
    i = 0
    while i < len(words):
        w = words[i]
        if w in _PREFIX or _ASSIGN.match(w):
            i += 1
        elif w == "sudo":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in ("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U") else 1
        elif w == "env":
            i += 1
            while i < len(words) and (words[i].startswith("-") or "=" in words[i]):
                i += 2 if words[i] in ("-u", "-C", "-S") else 1
        else:
            break
    return words[i:]


def _first_positional(args: list[str], value_opts: set[str] = frozenset()) -> str | None:
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return args[i + 1] if i + 1 < len(args) else None
        if a.startswith("-") and len(a) > 1:
            i += 2 if (a.lower() in value_opts and "=" not in a) else 1
            continue
        return a
    return None


def _is_ps(text: str) -> bool:
    low = text.lower()
    return (any(k in low for k in ("powershell", "pwsh", ".ps1", "cmd /c", "cmd.exe", ".bat", ".cmd"))
            or ("\\" in text and "/" not in text))


def _script_words(text: str, ps: bool, depth: int = 0) -> list[str]:
    """Words of a command line that name a file being executed (see _executed)."""
    if depth > 2:
        return []
    return _executed(shell_commands(text, powershell=ps), depth)


def _executed(commands: list[list[str]], depth: int = 0) -> list[str]:
    """The program itself when it is a path, or the script an interpreter (sh, bash, python,
    node, pwsh -File, cmd /c ...) is given, for each simple command. Nothing is expanded or
    evaluated."""
    out: list[str] = []
    for words in commands:
        words = _strip_prefix(words)
        if not words:
            continue
        head = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if head.endswith(".exe"):
            head = head[:-4]
        args = words[1:]
        if head in _SHELLS or _PY.fullmatch(head) or head in _INTERPRETERS:
            if "-c" in args:
                i = args.index("-c")
                if i + 1 < len(args) and head in _SHELLS:
                    out += _script_words(args[i + 1], False, depth + 1)
                continue
            if any(a in ("-m", "-e") for a in args) and head not in _SHELLS:
                continue
            p = _first_positional(args)
            if p:
                out.append(p)
        elif head in _PWSH:
            for i, a in enumerate(args):
                al = a.lower()
                if al in ("-file", "-f") and i + 1 < len(args):
                    out.append(args[i + 1])
                    break
                if al in ("-command", "-c") and i + 1 < len(args):
                    out += _script_words(" ".join(args[i + 1:]), True, depth + 1)
                    break
            else:
                p = _first_positional(args, _PWSH_VALUE)
                if p and p.lower().endswith(".ps1"):
                    out.append(p)
        elif head == "cmd":
            for i, a in enumerate(args):
                if a.lower() in ("/c", "/k") and i + 1 < len(args):
                    out += _script_words(" ".join(args[i + 1:]), True, depth + 1)
                    break
        elif "/" in words[0] or "\\" in words[0] or words[0].startswith("."):
            out.append(words[0])
    return out


# --------------------------------------------------------------------------------------------
# the pass

class _Pass:
    def __init__(self, root: Path, exclude: frozenset[str]):
        self.root = root
        self.base = root.resolve()
        self.exclude = exclude
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self.seen: set[tuple] = set()
        self.nodes_left = self.edges_left = self.work_left = 0

    # -- graph ---------------------------------------------------------------------------
    def add(self, nid: str, **attrs) -> str | None:
        cur = self.nodes.get(nid)
        if cur is None:
            if self.nodes_left <= 0:
                return None
            self.nodes_left -= 1
            self.nodes[nid] = {"id": nid, **{k: v for k, v in attrs.items() if v is not None}}
        else:
            for k, v in attrs.items():
                if v is not None and k not in cur:
                    cur[k] = v
        return nid

    def add_node(self, n: dict) -> str | None:
        return self.add(n["id"], **{k: v for k, v in n.items() if k != "id"})

    def edge(self, src, dst, rel, confidence="EXTRACTED", **attrs):
        if not src or not dst or src == dst or (src, dst, rel) in self.seen or self.edges_left <= 0:
            return
        self.edges_left -= 1
        self.seen.add((src, dst, rel))
        self.edges.append({"source": src, "target": dst, "relation": rel, "confidence": confidence,
                           **{k: v for k, v in attrs.items() if v is not None}})

    def spend(self, units: int) -> bool:
        self.work_left -= units
        return self.work_left >= 0

    def guarded(self, fn, *args):
        """Run one file's extraction with fresh caps; on an unforeseen shape drop what it added.
        The work share starts at the base; a reader that parses a document raises it by size."""
        self.nodes_left, self.edges_left = _MAX_NODES, _MAX_EDGES
        self.work_left = _WORK_BASE
        n0, e0 = len(self.nodes), len(self.edges)
        try:
            fn(*args)
        except Exception:
            for nid in list(self.nodes)[n0:]:
                del self.nodes[nid]
            for e in self.edges[e0:]:
                self.seen.discard((e["source"], e["target"], e["relation"]))
            del self.edges[e0:]

    def bundle(self, drel: str, src: str) -> str | None:
        key = drel or "."
        label = posixpath.basename(drel) if drel else self.base.name
        return self.add(f"beanstalkbundle:{key}", label=label, type="beanstalk_bundle",
                        file_type=_FT, source_file=src)

    def env(self, app: str | None, env: str, src: str) -> str | None:
        eid = self.add_node(env_node(app, env, src))
        if app and eid:
            self.edge(self.add_node(app_node(app, src)), eid, "has_environment")
        return eid

    # -- filesystem ----------------------------------------------------------------------
    def contained(self, p: Path, want: str = "file") -> bool:
        try:
            real = p.resolve(strict=True)
            if not real.is_relative_to(self.base):
                return False
            return real.is_file() if want == "file" else real.is_dir()
        except (OSError, RuntimeError, ValueError):
            return False

    def read(self, p: Path) -> str | None:
        try:
            if not self.contained(p) or p.stat().st_size > _MAX_BYTES:
                return None
            return p.read_text(encoding="utf-8", errors="replace").lstrip("﻿")
        except (OSError, RuntimeError, ValueError):
            return None

    def load(self, p: Path):
        """(parsed document, text) of a JSON or YAML file; (None, "") when unusable."""
        text = self.read(p)
        if text is None:
            return None, ""
        if text.lstrip().startswith(("{", "[")):
            try:
                return json.loads(text), text
            except (ValueError, RecursionError):
                pass
        try:
            return yaml.load(text, Loader=_Loader), text
        except Exception:
            return None, ""

    def files_in(self, d: Path) -> list[str]:
        try:
            with os.scandir(d) as it:
                return sorted(e.name for e in it if not e.is_dir(follow_symlinks=False))
        except OSError:
            return []

    def resolve_script(self, bundle: str, word: str) -> str | None:
        """A word from a command line -> repo-relative path of an existing local file, resolved
        against the bundle (the directory container_commands run in); None when unknowable."""
        w = word.strip().replace("\\", "/")
        for prefix in _STAGING:
            if w.startswith(prefix):
                w = w[len(prefix):]
                break
        rel = self.local(bundle, w)
        return rel if rel and self.contained(self.root / rel) else None

    def local(self, base: str, w: str) -> str | None:
        """Relative path `w` from repo directory `base`, lexically ("" = root); None when it is
        not a literal local path, leaves the repository or crosses an excluded name."""
        w = w.strip().replace("\\", "/")
        if (not w or len(w) > 512 or any(c in w for c in _UNSAFE_PATH) or w.startswith("/")
                or re.match(r"^[A-Za-z]:", w)):
            return None
        rel = posixpath.normpath(posixpath.join(base, w)) if base else posixpath.normpath(w)
        if rel == ".":
            return ""
        if rel == ".." or rel.startswith(("../", "/")):
            return None
        if any(part in self.exclude for part in rel.split("/")):
            return None
        return rel

    def script_edges(self, owner: str, bundle: str, text, section: str | None, command: str | None):
        if isinstance(text, list):          # exec form: one command, no shell
            words = [w for w in text if isinstance(w, str)]
            if not words or not self.spend(sum(len(w) for w in words) + 1):
                return
            found = _executed([words])
        elif isinstance(text, str) and text.strip():
            if not self.spend(len(text) + 1):
                return
            found = _script_words(text, _is_ps(text))
        else:
            return
        for w in found:
            rel = self.resolve_script(bundle, w)
            if rel:
                tid = self.add_node(file_node(rel))
                self.edge(owner, tid, "runs_script", "INFERRED", section=section, command=command)

    # -- .ebextensions -------------------------------------------------------------------
    def ebextension(self, p: Path, rel: str, bundle: str):
        data, text = self.load(p)
        if not isinstance(data, dict):
            return
        self.work_left = _WORK_BASE + _WORK_PER_BYTE * len(text)
        bid = self.bundle(bundle, rel)
        cid = self.add(f"beanstalkconfig:{rel}", label=posixpath.basename(rel),
                       type="beanstalk_config", file_type=_FT, source_file=rel)
        if not cid:
            return
        self.edge(bid, cid, "contains")
        node = self.nodes[cid]
        lists: dict[str, list[str]] = {}
        members: dict[str, set[str]] = {}        # the same names as sets: O(1) membership

        def note(key: str, value: str | None):
            if value is None:
                return
            lst = lists.setdefault(key, [])
            have = members.setdefault(key, set())
            if len(lst) < _MAX_NAMES and value not in have:
                lst.append(value)
                have.add(value)

        # option_settings: {namespace: {option: value}} or [{namespace, option_name, value}].
        # A YAML alias hands back the very same mapping object, so each one is read once, and a
        # (namespace, option) pair is kept once however often aliases repeat it.
        opts = data.get("option_settings")
        pairs: dict[tuple, None] = {}
        read_once: set = set()          # ids of mappings walked (the objects live in `data`)

        def pair(ns, opt):
            if isinstance(ns, str) and (opt is None or isinstance(opt, str)) \
                    and len(pairs) < 4 * _MAX_NAMES:
                pairs[(ns, opt)] = None

        if isinstance(opts, dict):
            items = [opts]
        elif isinstance(opts, list):
            items = opts
        else:
            items = []
        for item in items:
            if not self.spend(1) or not isinstance(item, dict) or id(item) in read_once:
                continue
            read_once.add(id(item))
            if "option_name" in item:
                pair(item.get("namespace", _ENV_NS), item.get("option_name"))
                continue
            for ns, body in item.items():
                if not self.spend(1):
                    break
                if isinstance(body, dict):
                    key = (ns, id(body)) if isinstance(ns, str) else None
                    if key is None or key in read_once:
                        continue
                    read_once.add(key)
                    for opt in body:
                        if not self.spend(1):
                            break
                        pair(ns, opt)
                elif body is None:
                    pair(ns, None)
        for ns, opt in pairs:
            if not self.spend(1):
                break
            ns_name = _name(ns, _NAMESPACE)
            if ns_name is None:
                continue
            note("namespaces", ns_name)
            if opt is None:
                continue
            if ns_name == _ENV_NS:
                note("environment_keys", _name(opt, _NAME))
            else:
                o = _option_name(opt)
                if o:
                    note("options", f"{ns_name}/{o}")

        pkgs = data.get("packages")
        if isinstance(pkgs, dict):
            for mgr, body in pkgs.items():
                m = _name(mgr, _MANAGER)
                if not m or not isinstance(body, dict):
                    continue
                for pkg in body:
                    if not self.spend(1):
                        break
                    n = _name(pkg, _PKG)
                    if n:
                        note("packages", f"{m}:{n}")

        for section in ("commands", "container_commands"):
            cmds = data.get(section)
            if not isinstance(cmds, dict):
                continue
            for name, body in cmds.items():
                if not self.spend(1):
                    break
                cname = _name(name if isinstance(name, str) else str(name), _CMD_NAME)
                if not cname:
                    continue
                note(section, cname)
                cmd = body.get("command") if isinstance(body, dict) else body
                self.script_edges(cid, bundle, cmd, section, cname)

        fls = data.get("files")
        if isinstance(fls, dict):
            for target in fls:
                if not self.spend(1):
                    break
                note("files", _fs_path_shape(target))

        svcs = data.get("services")
        if isinstance(svcs, dict):
            for mgr, body in svcs.items():
                m = _name(mgr, _MANAGER)
                if not m or not isinstance(body, dict):
                    continue
                for svc in body:
                    if not self.spend(1):
                        break
                    n = _name(svc, _PKG)
                    if n:
                        note("services", f"{m}:{n}")

        res = data.get("Resources")
        if isinstance(res, dict):
            ids: dict[str, str] = {}
            for lid, body in res.items():
                if not self.spend(1):
                    break
                name = _name(lid, _LOGICAL_ID)
                if not name:
                    continue
                rtype = _name(body.get("Type"), _RES_TYPE) if isinstance(body, dict) else None
                rid = self.add(f"beanstalkresource:{rel}#{name}", label=name,
                               type="beanstalk_resource", file_type=_FT, source_file=rel,
                               resource_type=rtype)
                if rid:
                    ids[name] = rid
                    self.edge(cid, rid, "defines_resource")
            for lid, body in res.items():
                if not isinstance(body, dict) or not isinstance(lid, str) or lid not in ids:
                    continue
                dep = body.get("DependsOn")
                for d in (dep if isinstance(dep, list) else [dep])[:_MAX_NAMES]:
                    if isinstance(d, str) and d in ids:
                        self.edge(ids[lid], ids[d], "depends_on")

        for key, lst in lists.items():
            if lst:
                node[key] = lst

    # -- .platform hooks -----------------------------------------------------------------
    def hooks(self, platform: Path, prel: str, bundle: str):
        for kind in _HOOK_KINDS:
            for phase in _HOOK_PHASES:
                d = platform / kind / phase
                if kind in self.exclude or phase in self.exclude or not self.contained(d, "dir"):
                    continue
                for fn in self.files_in(d)[:_MAX_HOOKS]:
                    rel = f"{prel}/{kind}/{phase}/{fn}"
                    if fn.startswith(".") or not self.contained(d / fn):
                        continue
                    bid = self.bundle(bundle, rel)
                    tid = self.add_node(file_node(rel))
                    self.edge(bid, tid, "runs", hook=kind, phase=phase)

    # -- .elasticbeanstalk/config.yml ----------------------------------------------------
    def eb_cli(self, p: Path, rel: str, bundle: str):
        data, _ = self.load(p)
        if not isinstance(data, dict):
            return
        g = data.get("global") if isinstance(data.get("global"), dict) else {}
        app = app_name(g.get("application_name"))
        bid = self.bundle(bundle, rel)
        cid = self.add(f"beanstalkcli:{rel}", label=posixpath.basename(rel), type="beanstalk_cli_config",
                       file_type=_FT, source_file=rel, application=app,
                       platform=_platform(g.get("default_platform")), region=_region(g.get("default_region")))
        if not cid:
            return
        self.edge(bid, cid, "contains")
        envs: dict[str, list[str]] = {}
        bd = data.get("branch-defaults")
        if isinstance(bd, dict):
            for branch, body in bd.items():
                if not self.spend(1):
                    break
                e = env_name(body.get("environment")) if isinstance(body, dict) else None
                if e:
                    lst = envs.setdefault(e, [])
                    b = _name(branch, _BRANCH)
                    if b and len(lst) < _MAX_NAMES:
                        lst.append(b)
        ed = data.get("environment-defaults")
        if isinstance(ed, dict):
            for e in ed:
                if not self.spend(1):
                    break
                e = env_name(e)
                if e:
                    envs.setdefault(e, [])
        if app:
            self.add_node(app_node(app, rel))
        for e, branches in envs.items():
            eid = self.env(app, e, rel)
            self.edge(cid, eid, "targets", branches=sorted(set(branches)) or None)
            self.edge(bid, eid, "deploys_to")

    # -- AWS .NET deployment-tool defaults -----------------------------------------------
    def project_for(self, drel: str, data: dict) -> str | None:
        """The .NET project a defaults file belongs to: the one `project-location` names (a
        project file, or a directory holding exactly one), else the single project file next
        to it. The tools read their defaults from the project directory."""
        base = drel
        loc = data.get("project-location") or data.get("project")
        if isinstance(loc, str) and loc.strip():
            cand = self.local(drel, loc)
            if cand is None:
                return None
            if manifest_kind(cand) == "msbuild" and self.contained(self.root / cand):
                return cand
            if not self.contained(self.root / cand if cand else self.root, "dir"):
                return None
            base = cand
        d = self.root / base if base else self.root
        projs = [n for n in self.files_in(d)
                 if manifest_kind(n) == "msbuild" and self.contained(d / n)]
        if len(projs) != 1:
            return None
        return f"{base}/{projs[0]}" if base else projs[0]

    def defaults(self, p: Path, rel: str, drel: str, tool: str):
        data, _ = self.load(p)
        if not isinstance(data, dict):
            return
        keys = []
        for k in data:
            if not self.spend(1) or len(keys) >= _MAX_NAMES:
                break
            n = _name(k, _NAME)
            if n:
                keys.append(n)
        is_eb = tool == "beanstalk"
        stack = _platform(data.get("solution-stack")) if is_eb else None
        did = self.add(f"awsdeploydefaults:{rel}", label=posixpath.basename(rel),
                       type="aws_deploy_defaults", file_type=_FT, source_file=rel, tool=tool,
                       option_keys=keys or None, region=_region(data.get("region")),
                       solution_stack=stack)
        if not did:
            return
        eid = bid = None
        if is_eb:
            app = app_name(data.get("application"))
            env = env_name(data.get("environment"))
            bid = self.bundle(drel, rel)
            self.edge(bid, did, "contains")
            if env:
                eid = self.env(app, env, rel)
                if eid and stack:
                    self.add(eid, solution_stack=stack)
                self.edge(did, eid, "targets")
                self.edge(bid, eid, "deploys_to", "INFERRED")
            elif app:
                self.add_node(app_node(app, rel))
        proj = self.project_for(drel, data)
        if proj:
            pid = self.add_node(_manifest_node(proj))
            self.edge(did, pid, "configures")
            if eid:
                self.edge(pid, eid, "deploys_to", "INFERRED")

    # -- Dockerrun.aws.json --------------------------------------------------------------
    def dockerrun(self, p: Path, rel: str, bundle: str):
        data, _ = self.load(p)
        if not isinstance(data, dict):
            return
        ver = data.get("AWSEBDockerrunVersion")
        ver = str(ver) if isinstance(ver, (int, float, str)) and str(ver) in ("1", "2", "3") else None
        bid = self.bundle(bundle, rel)

        def ports_of(lst, key):
            out = []
            for m in lst if isinstance(lst, list) else []:
                if not self.spend(1) or len(out) >= _MAX_NAMES:
                    break
                v = _port(m.get(key)) if isinstance(m, dict) else None
                if v is not None and v not in out:
                    out.append(v)
            return out or None

        def image(owner, ref):
            info = _image(ref)
            if not info:
                return
            iid = self.add(f"extimage:{info['name']}", label=info["name"], type="image",
                           file_type="docker", source_file=rel, repository=info["repository"],
                           tag=info.get("tag"))
            self.edge(owner, iid, "uses_image", digest=info.get("digest"),
                      tag_unresolved=info.get("tag_unresolved"))

        did = self.add(f"dockerrun:{rel}", label=posixpath.basename(rel), type="beanstalk_dockerrun",
                       file_type=_FT, source_file=rel, version=ver,
                       ports=ports_of(data.get("Ports"), "ContainerPort"))
        if not did:
            return
        self.edge(bid, did, "contains")
        img = data.get("Image")
        if isinstance(img, dict):
            image(did, img.get("Name"))
        defs = data.get("containerDefinitions")
        cids: dict[str, str] = {}
        links: list[tuple[str, object]] = []
        for c in defs if isinstance(defs, list) else []:
            if not self.spend(1) or not isinstance(c, dict):
                continue
            name = _name(c.get("name"), _CONTAINER)
            if not name:
                continue
            env = c.get("environment")
            keys = []
            for e in env if isinstance(env, list) else []:
                if not self.spend(1) or len(keys) >= _MAX_NAMES:
                    break
                k = _name(e.get("name"), _NAME) if isinstance(e, dict) else None
                if k and k not in keys:
                    keys.append(k)
            cid = self.add(f"dockerruncontainer:{rel}#{name}", label=name, type="beanstalk_container",
                           file_type=_FT, source_file=rel,
                           ports=ports_of(c.get("portMappings"), "containerPort"),
                           environment_keys=keys or None,
                           essential=c.get("essential") if isinstance(c.get("essential"), bool) else None)
            if not cid:
                continue
            cids[name] = cid
            self.edge(did, cid, "has_container")
            image(cid, c.get("image"))
            links.append((cid, c.get("links")))
        for cid, lst in links:
            for link in (lst if isinstance(lst, list) else [])[:_MAX_NAMES]:
                if isinstance(link, str):
                    other = link.split(":", 1)[0].strip()
                    if other in cids:
                        self.edge(cid, cids[other], "links")

    # -- Procfile ------------------------------------------------------------------------
    def procfile(self, p: Path, rel: str, bundle: str):
        text = self.read(p)
        if text is None:
            return
        bid = self.bundle(bundle, rel)
        for no, line in enumerate(text.splitlines()[:_MAX_NAMES], 1):
            if not self.spend(len(line) + 1):
                break
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            m = _PROCFILE_LINE.fullmatch(s)
            if not m:
                continue
            name = _name(m.group(1), _PROC)
            if not name:
                continue
            pid = self.add(f"beanstalkprocess:{rel}#{name}", label=name, type="beanstalk_process",
                           file_type=_FT, source_file=rel, source_location=f"L{no}")
            if pid:
                self.edge(bid, pid, "has_process")
                self.script_edges(pid, bundle, m.group(2), None, None)

    # -- one directory -------------------------------------------------------------------
    def directory(self, d: Path, drel: str, dirnames: list[str], filenames: list[str]):
        def rel(*parts):
            return "/".join(([drel] if drel else []) + list(parts))

        eb = {n for n in dirnames if n in _EB_DIRS and n not in self.exclude
              and self.contained(d / n, "dir")}
        if ".elasticbeanstalk" in eb and self.contained(d / ".elasticbeanstalk" / "config.yml"):
            p = d / ".elasticbeanstalk" / "config.yml"
            self.guarded(self.eb_cli, p, rel(".elasticbeanstalk", "config.yml"), drel)
        for fn in filenames:
            m = _DEFAULTS.fullmatch(fn)
            if m and self.contained(d / fn):
                self.guarded(self.defaults, d / fn, rel(fn), drel, m.group(1).lower())
        if ".ebextensions" in eb:
            x = d / ".ebextensions"
            for fn in self.files_in(x):
                if fn.lower().endswith(".config") and not fn.startswith("."):
                    self.guarded(self.ebextension, x / fn, rel(".ebextensions", fn), drel)
        if ".platform" in eb:
            self.guarded(self.hooks, d / ".platform", rel(".platform"), drel)
        if "Dockerrun.aws.json" in filenames:
            self.guarded(self.dockerrun, d / "Dockerrun.aws.json", rel("Dockerrun.aws.json"), drel)
        if "Procfile" in filenames and f"beanstalkbundle:{drel or '.'}" in self.nodes:
            self.guarded(self.procfile, d / "Procfile", rel("Procfile"), drel)


def extract(root: Path, exclude: frozenset[str] = frozenset(),
            include: frozenset[str] = frozenset()) -> dict:
    root = Path(root)
    ps = _Pass(root, frozenset(exclude))
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        drel = "" if d == root else d.relative_to(root).as_posix()
        names = sorted(dirnames)
        # .NET build output next to a project (`dotnet publish` / `dotnet eb` copy .ebextensions
        # into bin/ and obj/) is not a bundle of its own, as the ASP.NET pass prunes it too
        output = (_DOTNET_OUTPUT - include
                  if any(manifest_kind(f) == "msbuild" for f in filenames) else frozenset())
        # the Beanstalk directories are read in place, by fixed paths, never walked into
        dirnames[:] = [n for n in names if n not in _EB_DIRS and n not in output
                       and not skip_dir(n, exclude, include)]
        try:
            ps.directory(d, drel, names, sorted(filenames))
        except Exception:   # a directory listing that trips the pass costs that directory only
            continue
    return {"nodes": list(ps.nodes.values()), "edges": ps.edges}
