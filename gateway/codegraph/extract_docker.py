"""Docker extractor - which images a repository builds, from what, and which of its files go in.

A Dockerfile is not a language any other pass parses, and the interesting facts sit between
files: a stage built on another stage, a script copied in and then run as the entrypoint, a
compose service built from one Dockerfile and mounting a config file, a release script that
builds and pushes. This repo-level pass reads (stdlib + PyYAML, no tree-sitter, no daemon):

  Dockerfile, Containerfile, *.Dockerfile, Dockerfile.* (and the Containerfile spellings)
      FROM, COPY/ADD, WORKDIR, ENTRYPOINT/CMD/RUN, EXPOSE, ENV/ARG (names)
  compose.y[a]ml, compose.*.y[a]ml, docker-compose*.y[a]ml
      services: image, build (context/dockerfile/target), depends_on, volumes (bind mounts),
      env_file, environment (keys)
  *.sh / *.bash / *.zsh / *.ps1 / *.psm1
      `docker|podman build ... [-f <file>] <context>` and `docker|podman push <image>`

Emitted nodes:
  dockerfile:<rel>                a Dockerfile; build_args (global ARG names)
  dockerstage:<rel>#<stage>       a build stage, by its AS name or its index; stage_index, base
                                  (the literal FROM operand), expose, env and args (NAMES), final
  extimage:<name[:tag]>           an external image; repository, tag. A digest goes on the edge
  compose:<rel>                   a compose file
  composeservice:<rel>#<service>  a service; environment_keys, env_files, build_args (names)
  module:<rel> / file:<rel>       a local file copied, run or mounted: module:<rel> (the id the
                                  python / tree-sitter passes use) for code and scripts, the
                                  manifests pass's project:/solution:/manifest: id for a
                                  manifest, dockerfile:<rel> for a Dockerfile, file:<rel> else

Emitted edges: has_stage (dockerfile -> stage), based_on (stage -> extimage or prior stage;
digest attr), copies (stage -> local file), copies_from (stage -> stage or extimage, COPY
--from), runs (stage -> the local file an ENTRYPOINT/CMD/RUN executes, found by following COPY
destinations and WORKDIR; instruction attr), has_service, uses_image, builds_from (service ->
dockerfile; stage attr), targets_stage (service -> the stage `build.target` names), depends_on,
mounts (service -> bind-mounted local file; container_path and read_only attrs), uses_env_file,
and builds / pushes (script -> dockerfile, stage attr from --target / extimage, also for
`buildx build --push -t`).

Build context: COPY sources are relative to the context, which the Dockerfile itself never
names. Contexts declared by a compose file resolve EXTRACTED; contexts from a script (relative
to an unknown working directory: the script's directory, then the repository root) and the
default (the Dockerfile's own directory) are INFERRED, as is every builds edge from a script.

Deliberately absent: every VALUE. No ENV/ARG values or defaults, no --build-arg values, no
environment values, no env_file contents (never read), no labels, no RUN command text, no
secrets/configs sections, no remote ADD/build-context URLs. Variable references are never
expanded (a tag built from ${VERSION} becomes the untagged repository plus tag_unresolved);
a directory copy maps paths for `runs` but emits no edge of its own. No network, no daemon, no
subprocess, no template evaluation. A COPY wildcard is expanded inside the repository only,
never through a directory symlink, with capped segments, listings and matches; a compose file
lists at most MAX_LISTED_NAMES names and walks at most MAX_COMPOSE_ITEMS entries, so a YAML
alias shared by hundreds of services cannot multiply its output.
"""
from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import shlex
from pathlib import Path

import yaml

from .extract_ansible import skip_dir
from .extract_manifests import node_for as _manifest_node
from .treesitter import EXT_LANG

MAX_BYTES = 2 * 1024 * 1024   # a larger Dockerfile/compose/script is generated, not authored
MAX_STAGES = 128              # build stages per Dockerfile
MAX_EDGES = 4000              # edges per file
MAX_SERVICES = 500            # services per compose file
MAX_GLOB = 200                # files one COPY wildcard may expand to
MAX_GLOB_SEGMENTS = 4         # wildcard path segments in one COPY source
MAX_GLOB_SCANS = 2000         # directories one COPY wildcard may list
MAX_LISTED_NAMES = 20_000     # environment/build-arg/env_file names one compose file may list
MAX_COMPOSE_ITEMS = 200_000   # environment, volume, env_file and depends_on entries walked per file
_SCRIPT_LANG = {".sh": "bash", ".bash": "bash", ".zsh": "bash",
                ".ps1": "powershell", ".psm1": "powershell"}
_DOCKER_BINS = {"docker", "podman", "nerdctl"}
_GLOBAL_VALUE_FLAGS = {"-H", "--host", "--context", "-c", "--config", "-l", "--log-level",
                       "--tlscacert", "--tlscert", "--tlskey", "--url", "--connection"}
# `docker build` flags that consume the next word. A flag missing here is treated as boolean, so
# its value would be mistaken for the context - which then simply fails to resolve to a file.
_BUILD_VALUE_FLAGS = {
    "-f", "--file", "-t", "--tag", "--build-arg", "--target", "--platform", "--label",
    "--secret", "--ssh", "--cache-from", "--cache-to", "--network", "--progress", "-o",
    "--output", "--iidfile", "--build-context", "--add-host", "--metadata-file", "--shm-size",
    "--ulimit", "--allow", "--annotation", "--attest", "--builder", "--cgroup-parent",
    "--isolation", "-m", "--memory", "--memory-swap", "--cpu-period", "--cpu-quota",
    "--cpu-shares", "-c", "--cpuset-cpus", "--cpuset-mems", "--security-opt", "--call",
    "--format", "--authfile", "--creds", "--cert-dir", "-v", "--volume", "-e", "--env",
    "--arch", "--os", "--variant", "--userns", "--jobs", "--layers", "--sign-by", "--timestamp",
    "--build-context", "--cache-ttl", "--manifest", "--retry", "--retry-delay", "--logfile",
}
_PUSH_VALUE_FLAGS = {"--platform", "--authfile", "--creds", "--cert-dir", "--format",
                     "--digestfile", "--sign-by", "--retry", "--retry-delay"}
_INTERPRETERS = {"sh", "bash", "zsh", "ash", "dash", "python", "python3", "pwsh", "powershell",
                 "node", "ruby", "perl", "source", ".", "exec", "uv", "poetry", "pipenv"}
_IMAGE_REPO = re.compile(r"^[a-z0-9][a-z0-9._/:-]*[a-z0-9]$|^[a-z0-9]$")
_TAG = re.compile(r"^[\w][\w.-]{0,127}$")
_DIGEST = re.compile(r"^[A-Za-z0-9_+.-]+:[A-Fa-f0-9]{32,}$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_COMPOSE = re.compile(r"^(docker-compose.*|compose(\..+)?)\.ya?ml$", re.I)


# --------------------------------------------------------------------------------------------
# classification and paths

def is_dockerfile(name: str) -> bool:
    n = name.lower()
    if n.endswith((".dockerignore", ".containerignore")):
        return False
    return (n in ("dockerfile", "containerfile") or n.endswith((".dockerfile", ".containerfile"))
            or n.startswith(("dockerfile.", "containerfile.")))


def is_compose(name: str) -> bool:
    return bool(_COMPOSE.match(name))


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


def _join(base: str, p) -> str | None:
    """Relative path `p` from repo directory `base` as a repo-relative POSIX path ("" = root);
    None when it is not a literal local path or would leave the repository."""
    if not isinstance(p, str):
        return None
    p = p.strip().replace("\\", "/")
    if (not p or "$" in p or "://" in p or "%" in p or p.startswith(("/", "~", "git@"))
            or re.match(r"^[A-Za-z]:", p)):
        return None
    out = posixpath.normpath(posixpath.join(base, p))
    if out == ".":
        return ""
    if out == ".." or out.startswith(("../", "/")):
        return None
    return out


def _kind(root_res: Path, rel: str) -> str | None:
    """'file' / 'dir' for a path inside the repository (after resolving links), else None."""
    try:
        real = (root_res / rel).resolve(strict=True) if rel else root_res
        if not real.is_relative_to(root_res):
            return None
        return "file" if real.is_file() else "dir" if real.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _glob(root_res: Path, ctx: str, pattern: str) -> list[str]:
    """Repo-relative matches of a COPY/ADD wildcard below context `ctx`, sorted, at most
    MAX_GLOB. Expanded one segment at a time with os.scandir, never through a directory
    symlink (Path.glob follows them, out of the root and round a `loop -> .` link), stopping at
    the first MAX_GLOB matches, and with at most MAX_GLOB_SEGMENTS wildcard segments."""
    segs = [p for p in pattern.split("/") if p not in ("", ".")]
    if not segs or sum(1 for p in segs if any(c in p for c in "*?[")) > MAX_GLOB_SEGMENTS:
        return []
    try:
        base = (root_res / ctx).resolve() if ctx else root_res
        if not base.is_relative_to(root_res) or not base.is_dir():
            return []
    except (OSError, RuntimeError, ValueError):
        return []
    out: list[str] = []
    scans = [0]

    def walk(d: Path, rel: str, i: int):
        if len(out) >= MAX_GLOB or scans[0] >= MAX_GLOB_SCANS:
            return
        seg, last = segs[i], i == len(segs) - 1
        if not any(c in seg for c in "*?["):
            names = [seg]
        else:
            scans[0] += 1
            try:
                with os.scandir(d) as it:
                    names = sorted(e.name for e in it if fnmatch.fnmatchcase(e.name, seg))
            except OSError:
                return
        for name in names:
            if len(out) >= MAX_GLOB:
                return
            p = d / name
            r = f"{rel}/{name}" if rel else name
            if last:
                if os.path.lexists(p):
                    out.append(r)       # a file link is kept; _kind then checks where it points
                continue
            try:
                if p.is_symlink() or not p.is_dir():
                    continue
            except OSError:
                continue
            walk(p, r, i + 1)

    walk(base, base.relative_to(root_res).as_posix() if base != root_res else "", 0)
    return out


def _dockerfile_node(rel: str) -> dict:
    return {"id": f"dockerfile:{rel}", "label": posixpath.basename(rel), "type": "dockerfile",
            "file_type": "docker", "source_file": rel}


def file_node(rel: str) -> dict:
    """The node another pass already gives `rel`, so the graphs join; file:<rel> otherwise."""
    m = _manifest_node(rel)
    if m:
        return m
    name = posixpath.basename(rel)
    if is_dockerfile(name):
        return _dockerfile_node(rel)
    ext = posixpath.splitext(name)[1]
    lang = "python" if ext == ".py" else EXT_LANG.get(ext.lower())
    if lang:
        return {"id": f"module:{rel}", "label": rel, "type": "module", "file_type": lang,
                "source_file": rel}
    return {"id": f"file:{rel}", "label": name, "type": "file", "file_type": "file",
            "source_file": rel}


def parse_image(ref) -> dict | None:
    """{name, repository, tag?, tag_unresolved?, digest?} of an image reference; None for scratch,
    a reference whose repository is a variable, or anything that is not an image name."""
    if not isinstance(ref, str):
        return None
    ref = ref.strip()
    if not ref or ref.lower() == "scratch":
        return None
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    repo, tag = ref, None
    slash = ref.rfind("/")
    if ":" in ref[slash + 1:]:
        cut = ref.index(":", slash + 1)     # first colon of the last segment: `app:${V:-1}`
        repo, tag = ref[:cut], ref[cut + 1:]
    if not repo or "$" in repo or not _IMAGE_REPO.match(repo) or "//" in repo:
        return None
    info: dict = {"repository": repo}
    if tag is not None:
        if "$" not in tag and _TAG.match(tag):
            info["tag"] = tag
        else:
            info["tag_unresolved"] = True
    if digest and _DIGEST.match(digest):
        info["digest"] = digest
    info["name"] = repo + (f":{info['tag']}" if "tag" in info else "")
    return info


# --------------------------------------------------------------------------------------------
# tokenizers

def shell_commands(text: str, powershell: bool = False) -> list[list[str]]:
    """Split shell text into simple commands (lists of unquoted words). Separators are ; & | and
    newlines outside quotes and $( ); comments are dropped; nothing is expanded."""
    text = re.sub(r"`\r?\n" if powershell else r"\\\r?\n", " ", text)
    cmds: list[list[str]] = []
    words: list[str] = []
    cur: list[str] = []
    started = False
    quote = None
    depth = 0
    i, n = 0, len(text)

    def flush_word():
        nonlocal cur, started
        if started:
            words.append("".join(cur))
        cur, started = [], False

    def flush_cmd():
        nonlocal words
        flush_word()
        if words:
            cmds.append(words)
        words = []

    while i < n:
        c = text[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and not powershell and i + 1 < n:
                cur.append(text[i + 1])
                i += 1
            else:
                cur.append(c)
        elif depth:
            cur.append(c)
            depth += 1 if c == "(" else -1 if c == ")" else 0
        elif c in "'\"":
            quote, started = c, True
        elif c == "$" and text[i + 1:i + 2] == "(":
            cur.append("$(")
            depth, started = 1, True
            i += 1
        elif c == "#" and not started:
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif c in " \t\r":
            flush_word()
        elif c in ";&|\n":
            flush_cmd()
        elif c == "\\" and not powershell and i + 1 < n:
            cur.append(text[i + 1])
            started = True
            i += 1
        else:
            cur.append(c)
            started = True
        i += 1
    flush_cmd()
    return cmds


def _instructions(text: str):
    """(line, KEYWORD, args) per Dockerfile instruction: continuations joined, comment lines and
    heredoc bodies dropped, the `# escape=` parser directive honoured."""
    lines = text.splitlines()
    escape = "\\"
    for ln in lines:
        m = re.match(r"^\s*#\s*([A-Za-z]+)\s*=\s*(\S+)\s*$", ln)
        if not m:
            break
        if m.group(1).lower() == "escape" and m.group(2) in ("`", "\\"):
            escape = m.group(2)
    out = []
    i, n = 0, len(lines)
    while i < n:
        start = i + 1
        cur = lines[i].rstrip()
        i += 1
        if not cur.strip() or cur.lstrip().startswith("#"):
            continue
        buf = []
        while cur.endswith(escape):
            buf.append(cur[:-1])
            while i < n and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
                i += 1
            if i >= n:
                cur = ""
                break
            cur = lines[i].rstrip()
            i += 1
        buf.append(cur)
        line = " ".join(b.strip() for b in buf if b.strip())
        m = re.match(r"^([A-Za-z]+)\s*(.*)$", line)
        if not m:
            continue
        kw, args = m.group(1).upper(), m.group(2)
        if kw in ("RUN", "COPY", "ADD"):
            for hm in re.finditer(r"<<(-?)([\"']?)([A-Za-z_]\w*)\2", args):
                delim = hm.group(3)
                while i < n:
                    body = lines[i]
                    i += 1
                    if body.strip() == delim:
                        break
        out.append((start, kw, args))
    return out


def _flags(args: str) -> tuple[dict, str]:
    flags: dict[str, str] = {}
    rest = args.strip()
    while rest.startswith("--"):
        m = re.match(r"(--\S+)\s*(.*)$", rest, re.S)
        if not m:
            break
        k, _, v = m.group(1)[2:].partition("=")
        flags[k.lower()] = v
        rest = m.group(2)
    return flags, rest


def _words(rest: str) -> tuple[list[str], bool]:
    s = rest.strip()
    if s.startswith("["):
        try:
            v = json.loads(s)
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                return v, True
        except ValueError:
            pass
    return s.split(), False


def _names(args: str, legacy_env: bool = False) -> list[str]:
    """Variable NAMES declared by ENV/ARG; the values are parsed only to be thrown away."""
    try:
        words = shlex.split(args, posix=True)
    except ValueError:
        words = args.split()
    if not words:
        return []
    if legacy_env and "=" not in words[0]:
        return [words[0]] if _NAME.match(words[0]) else []        # `ENV KEY some value`
    out = []
    for w in words:
        k = w.split("=", 1)[0]
        if _NAME.match(k):
            out.append(k)
    return out


def _img_path(workdir: str | None, p: str) -> str | None:
    if not p or "$" in p:
        return None
    if p.startswith("/"):
        return posixpath.normpath(p)
    if workdir is None:
        return None
    return posixpath.normpath(posixpath.join(workdir, p))


# --------------------------------------------------------------------------------------------
# the pass

def extract(root: Path, exclude: frozenset[str] = frozenset(),
            include: frozenset[str] = frozenset()) -> dict:
    root = Path(root)
    root_res = root.resolve()
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    seen: set[tuple] = set()

    def add(n: dict) -> str:
        cur = nodes.setdefault(n["id"], dict(n))
        for k, v in n.items():
            if v is not None and k not in cur:
                cur[k] = v
        return n["id"]

    def edge(src, dst, rel, confidence="EXTRACTED", **attrs) -> bool:
        attrs = {k: v for k, v in attrs.items()
                 if v is not None and k not in ("source", "target", "relation", "confidence")}
        key = (src, dst, rel, tuple(sorted((k, str(v)) for k, v in attrs.items())))
        if key in seen:
            return False
        seen.add(key)
        edges.append({"source": src, "target": dst, "relation": rel, "confidence": confidence, **attrs})
        return True

    def image(info: dict, rel: str) -> str:
        node = {"id": f"extimage:{info['name']}", "label": info["name"], "type": "image",
                "file_type": "docker", "source_file": rel, "repository": info["repository"]}
        if "tag" in info:
            node["tag"] = info["tag"]
        return add(node)

    dockerfiles: list[tuple[Path, str]] = []
    composes: list[tuple[Path, str]] = []
    scripts: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not skip_dir(d, exclude, include))
        d = Path(dirpath)
        drel = "" if d == root else d.relative_to(root).as_posix()
        for fn in sorted(filenames):
            rel = f"{drel}/{fn}" if drel else fn
            if is_dockerfile(fn):
                dockerfiles.append((d / fn, rel))
            elif is_compose(fn):
                composes.append((d / fn, rel))
            elif posixpath.splitext(fn)[1].lower() in _SCRIPT_LANG:
                scripts.append((d / fn, rel))

    # Build contexts per Dockerfile: (context rel, confidence). Collected from compose files and
    # scripts first, because a COPY source means nothing until its context is known.
    contexts: dict[str, list[tuple[str, str]]] = {}
    pending: list = []    # edges whose target (a stage) exists only once Dockerfiles are parsed

    def add_context(df: str, ctx: str, conf: str):
        lst = contexts.setdefault(df, [])
        if (ctx, conf) not in lst and not (conf == "INFERRED" and (ctx, "EXTRACTED") in lst):
            lst.append((ctx, conf))

    # ---- compose ----------------------------------------------------------------------------
    for path, rel in composes:
        text = _read(root_res, path)
        if text is None:
            continue
        try:
            data = yaml.safe_load(text)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        services = data.get("services")
        if not isinstance(services, dict):
            # legacy v1 layout: services at the top level
            services = {k: v for k, v in data.items()
                        if isinstance(v, dict) and ("image" in v or "build" in v)}
        if not services:
            continue
        base = posixpath.dirname(rel)
        cid = add({"id": f"compose:{rel}", "label": posixpath.basename(rel), "type": "compose_file",
                   "file_type": "compose", "source_file": rel})
        names = [s for s in services if isinstance(s, str)][:MAX_SERVICES]
        budget = [MAX_EDGES]

        def cedge(*a, **kw):
            if budget[0] > 0 and edge(*a, **kw):
                budget[0] -= 1

        # A YAML alias lets one environment map or volume list stand in hundreds of services:
        # cap the entries walked and the names listed per file, not just the edges.
        items_left = [MAX_COMPOSE_ITEMS]
        names_left = [MAX_LISTED_NAMES]

        def listed(xs: list[str]) -> list[str]:
            xs = list(dict.fromkeys(xs))[:max(0, names_left[0])]
            names_left[0] -= len(xs)
            return xs

        for svc in names:
            spec = services[svc] if isinstance(services[svc], dict) else {}
            items_left[0] -= 1 + sum(len(spec.get(k)) for k in
                                     ("environment", "env_file", "volumes", "depends_on", "build")
                                     if isinstance(spec.get(k), (dict, list)))
            if items_left[0] < 0:
                break
            sid = f"composeservice:{rel}#{svc}"
            node = {"id": sid, "label": svc, "type": "compose_service", "file_type": "compose",
                    "source_file": rel}
            cedge(cid, sid, "has_service")

            env = spec.get("environment")
            keys: list[str] = []
            if isinstance(env, dict):
                keys = [k for k in env if isinstance(k, str) and _NAME.match(k)]
            elif isinstance(env, list):
                keys = [e.split("=", 1)[0] for e in env if isinstance(e, str) and _NAME.match(e.split("=", 1)[0])]
            keys = listed(keys)
            if keys:
                node["environment_keys"] = keys

            ef = spec.get("env_file")
            ef_list = ef if isinstance(ef, list) else [ef] if ef is not None else []
            env_files = []
            for item in ef_list:
                p = item.get("path") if isinstance(item, dict) else item
                target = _join(base, p)
                if target is None:
                    continue
                env_files.append(target)
                if _kind(root_res, target) == "file":   # linked by name; never read
                    cedge(sid, add(file_node(target)), "uses_env_file")
            env_files = listed(env_files)
            if env_files:
                node["env_files"] = env_files

            img = parse_image(spec.get("image"))
            build = spec.get("build")
            if img:
                cedge(sid, image(img, rel), "uses_image", digest=img.get("digest"),
                      tag_unresolved=img.get("tag_unresolved"), built=True if build else None)

            if isinstance(build, (str, dict)):
                b = {"context": build} if isinstance(build, str) else build
                ctx = _join(base, b.get("context", "."))
                if ctx is not None and _kind(root_res, ctx) == "dir" and not b.get("dockerfile_inline"):
                    dfp = b.get("dockerfile") or "Dockerfile"
                    df = _join(ctx, dfp)
                    if df and _kind(root_res, df) == "file":
                        add_context(df, ctx, "EXTRACTED")
                        add(_dockerfile_node(df))
                        target = b.get("target") if isinstance(b.get("target"), str) else None
                        cedge(sid, f"dockerfile:{df}", "builds_from",
                              stage=target if target and _NAME.match(target) else None)
                        if target:
                            pending.append((sid, f"dockerstage:{df}#{target.lower()}", "targets_stage"))
                bargs = b.get("args")
                bnames = ([k for k in bargs if isinstance(k, str)] if isinstance(bargs, dict) else
                          [a.split("=", 1)[0] for a in bargs if isinstance(a, str)] if isinstance(bargs, list) else [])
                bnames = listed([k for k in bnames if _NAME.match(k)])
                if bnames:
                    node["build_args"] = bnames

            dep = spec.get("depends_on")
            deps = list(dep) if isinstance(dep, (dict, list)) else []
            for other in deps:
                if isinstance(other, str) and other in services:
                    cedge(sid, f"composeservice:{rel}#{other}", "depends_on")

            vols = spec.get("volumes")
            for vol in vols if isinstance(vols, list) else []:
                src = tgt = None
                ro = None
                if isinstance(vol, str):
                    parts = vol.split(":")
                    if len(parts) >= 2 and re.match(r"^[A-Za-z]$", parts[0]):  # C:\... host path
                        continue
                    src = parts[0]
                    tgt = parts[1] if len(parts) >= 2 else None
                    ro = True if len(parts) >= 3 and "ro" in parts[2].split(",") else None
                elif isinstance(vol, dict) and vol.get("type", "bind") == "bind":
                    src, tgt = vol.get("source"), vol.get("target")
                    ro = True if vol.get("read_only") is True else None
                if not isinstance(src, str) or not src.startswith("."):
                    continue          # named volumes, absolute host paths, variables
                target = _join(base, src)
                if target and _kind(root_res, target) == "file":
                    cedge(sid, add(file_node(target)), "mounts",
                          container_path=tgt if isinstance(tgt, str) and "$" not in tgt else None,
                          read_only=ro)
            add(node)

    # ---- scripts ----------------------------------------------------------------------------
    for path, rel in scripts:
        text = _read(root_res, path)
        if text is None:
            continue
        ps = _SCRIPT_LANG[posixpath.splitext(rel)[1].lower()] == "powershell"
        try:
            cmds = shell_commands(text, powershell=ps)
        except Exception:
            continue
        mid = f"module:{rel}"
        sdir = posixpath.dirname(rel)
        budget = MAX_EDGES
        for words in cmds:
            if budget <= 0:
                break
            idx = next((k for k, w in enumerate(words)
                        if re.split(r"[\\/]", w)[-1].lower().removesuffix(".exe") in _DOCKER_BINS), None)
            if idx is None:
                continue
            args = words[idx + 1:]
            while args and args[0].startswith("-"):           # global flags before the subcommand
                flag = args.pop(0)
                if flag in _GLOBAL_VALUE_FLAGS and "=" not in flag and args:
                    args.pop(0)
            sub = []
            while args and len(sub) < 2 and args[0] in ("build", "buildx", "image", "builder", "push"):
                sub.append(args.pop(0))
                if sub[-1] in ("build", "push"):
                    break
            if not sub or sub[-1] not in ("build", "push"):
                continue
            opts: dict[str, list[str]] = {}
            positional: list[str] = []
            value_flags = _BUILD_VALUE_FLAGS if sub[-1] == "build" else _PUSH_VALUE_FLAGS
            while args:
                w = args.pop(0)
                if w.startswith("-") and w != "-":
                    k, eq, v = w.partition("=")
                    if not eq and k in value_flags and args:
                        v = args.pop(0)
                    opts.setdefault(k, []).append(v if (eq or k in value_flags) else "")
                else:
                    positional.append(w)
            add(file_node(rel))
            if sub[-1] == "push":
                if positional:
                    img = parse_image(positional[0])
                    if img and edge(mid, image(img, rel), "pushes", digest=img.get("digest"),
                                    tag_unresolved=img.get("tag_unresolved")):
                        budget -= 1
                continue
            ctx_arg = positional[0] if positional else None
            fflag = (opts.get("-f") or opts.get("--file") or [None])[-1]
            if ctx_arg in (None, "-") or (fflag == "-"):
                continue
            for cwd in dict.fromkeys([sdir, ""]):              # unknown cwd: script dir, then root
                ctx = _join(cwd, ctx_arg)
                if ctx is None or _kind(root_res, ctx) != "dir":
                    continue
                df = _join(cwd, fflag) if fflag else _join(ctx, "Dockerfile")
                if df and _kind(root_res, df) == "file":
                    add_context(df, ctx, "INFERRED")
                    add(_dockerfile_node(df))
                    tgt = (opts.get("--target") or [None])[-1]
                    if edge(mid, f"dockerfile:{df}", "builds", "INFERRED",
                            stage=tgt if tgt and _NAME.match(tgt) else None):
                        budget -= 1
                    if "--push" in opts:
                        for t in (opts.get("-t", []) + opts.get("--tag", [])):
                            img = parse_image(t)
                            if img and edge(mid, image(img, rel), "pushes",
                                            tag_unresolved=img.get("tag_unresolved")):
                                budget -= 1
                    break

    # ---- Dockerfiles ------------------------------------------------------------------------
    for path, rel in dockerfiles:
        text = _read(root_res, path)
        if text is None:
            continue
        try:
            _dockerfile(rel, text, contexts.get(rel) or [(posixpath.dirname(rel), "INFERRED")],
                        root_res, add, edge, image)
        except Exception:      # one malformed Dockerfile never fails the build
            continue

    for src, dst, rel_name in pending:
        if dst in nodes:
            edge(src, dst, rel_name)

    return {"nodes": list(nodes.values()), "edges": edges}


def _dockerfile(rel, text, contexts, root_res, add, edge, image):
    did = f"dockerfile:{rel}"
    df_node = _dockerfile_node(rel)
    stages: list[dict] = []
    by_name: dict[str, dict] = {}
    global_args: list[str] = []
    cur = None
    budget = [MAX_EDGES]

    def fedge(*a, **kw):
        if budget[0] > 0 and edge(*a, **kw):
            budget[0] -= 1

    def lookup(st, ip, want_dir=False):
        """(kind, repo rel, confidence) of image path `ip` in stage `st`, via its COPY map."""
        if ip is None:
            return None
        if not want_dir and ip in st["files"]:
            return ("file",) + st["files"][ip]
        best = None
        for d, val in st["dirs"].items():
            prefix = d if d.endswith("/") else d + "/"
            if (ip == d or ip.startswith(prefix)) and (best is None or len(d) > len(best[0])):
                best = (d, val)
        if best:
            d, (drel, conf) = best
            rest = ip[len(d):].lstrip("/")
            cand = _join(drel, rest) if rest else drel
            if cand is not None:
                k = _kind(root_res, cand)
                if k and (want_dir or k == "file"):
                    return (k, cand, conf)
        return None

    def register(st, dest_abs, is_dir_dest, kind, srel, conf):
        if dest_abs is None:
            return
        if kind == "dir":
            st["dirs"][dest_abs] = (srel, conf)
        else:
            base = posixpath.basename(srel)
            st["files"][posixpath.join(dest_abs, base)] = (srel, conf)
            if not is_dir_dest:
                st["files"][dest_abs] = (srel, conf)

    def runs(st, tokens, instruction, restricted):
        cands: list[str] = []
        if restricted:          # RUN: only what is executed, not every argument (chmod +x x.sh)
            for words in tokens:
                if not words:
                    continue
                cands.append(words[0])
                if posixpath.basename(words[0]) in _INTERPRETERS:
                    rest = [w for w in words[1:] if not w.startswith("-")]
                    if rest and rest[0] in ("run", "exec") and len(rest) > 1:
                        rest = rest[1:]
                    if rest:
                        cands.append(rest[0])
        else:
            cands = [w for words in tokens for w in words]
        for tok in cands:
            if not tok or tok.startswith("-") or "$" in tok:
                continue
            hit = lookup(st, _img_path(st["workdir"], tok))
            if hit and hit[0] == "file":
                fedge(st["id"], add(file_node(hit[1])), "runs", hit[2], instruction=instruction)

    for line_no, kw, args in _instructions(text):
        if kw == "FROM":
            if len(stages) >= MAX_STAGES:
                break
            _, rest = _flags(args)
            words = rest.split()
            if not words:
                continue
            base = words[0]
            name = words[2] if len(words) >= 3 and words[1].lower() == "as" else None
            idx = len(stages)
            sid = f"dockerstage:{rel}#{name.lower() if name else idx}"
            st = {"id": sid, "workdir": "/", "files": {}, "dirs": {}, "expose": [], "env": [], "args": [],
                  "node": {"id": sid, "label": name or f"stage {idx}", "type": "docker_stage",
                           "file_type": "docker", "source_file": rel, "source_location": f"L{line_no}",
                           "stage_index": idx, "base": base if len(base) <= 200 else None}}
            parent = by_name.get(base.lower())
            if parent is not None:
                st["workdir"] = parent["workdir"]
                st["files"] = dict(parent["files"])
                st["dirs"] = dict(parent["dirs"])
                fedge(sid, parent["id"], "based_on")
            else:
                img = parse_image(base)
                if img:
                    fedge(sid, image(img, rel), "based_on", digest=img.get("digest"),
                          tag_unresolved=img.get("tag_unresolved"))
            fedge(did, sid, "has_stage")
            stages.append(st)
            by_name[str(idx)] = st
            if name:
                by_name[name.lower()] = st
            cur = st
            continue
        if cur is None:
            if kw == "ARG":
                global_args += _names(args)
            continue
        if kw == "WORKDIR":
            w = args.strip().strip('"').strip("'")
            # Once unknown (`WORKDIR $VAR`), a relative WORKDIR stays unknown: only an absolute
            # one resets it. Resolving it against "/" would invent a base the image never has.
            cur["workdir"] = _img_path(cur["workdir"], w) if w else cur["workdir"]
        elif kw in ("COPY", "ADD"):
            flags, rest = _flags(args)
            words, _ = _words(rest)
            if len(words) < 2:
                continue
            srcs, dest = words[:-1], words[-1]
            dest_abs = _img_path(cur["workdir"], dest)
            is_dir_dest = dest.endswith("/") or dest in (".", "./") or len(srcs) > 1
            frm = flags.get("from")
            if frm:
                other = by_name.get(frm.lower())
                if other is not None and other is not cur:
                    fedge(cur["id"], other["id"], "copies_from")
                    for s in srcs:
                        hit = lookup(other, posixpath.normpath("/" + s.lstrip("/")))
                        if hit is None:
                            hit = lookup(other, posixpath.normpath("/" + s.lstrip("/")), want_dir=True)
                        if hit:
                            register(cur, dest_abs, is_dir_dest, hit[0], hit[1], hit[2])
                else:
                    img = parse_image(frm)
                    if img:
                        fedge(cur["id"], image(img, rel), "copies_from", digest=img.get("digest"))
                continue
            for ctx, conf in contexts:
                for s in srcs:
                    if s.startswith("<<") or "://" in s or s.startswith("git@") or "$" in s:
                        continue       # heredoc, remote URL or a variable: nothing local to link
                    s = s.lstrip("/") or "."
                    if any(c in s for c in "*?["):
                        if ".." in s.split("/") or "**" in s:
                            continue   # never glob out of the context, never recurse
                        for mrel in _glob(root_res, ctx, s):
                            if _kind(root_res, mrel) == "file":
                                fedge(cur["id"], add(file_node(mrel)), "copies", conf)
                                register(cur, dest_abs, True, "file", mrel, conf)
                        continue
                    srel = _join(ctx, s)
                    if srel is None:
                        continue
                    k = _kind(root_res, srel)
                    if k == "file":
                        fedge(cur["id"], add(file_node(srel)), "copies", conf)
                    if k:
                        register(cur, dest_abs, is_dir_dest, k, srel, conf)
        elif kw in ("ENTRYPOINT", "CMD", "RUN"):
            words, exec_form = _words(args)
            if exec_form:
                tokens = []
                for w in words:
                    tokens += shell_commands(w) if (" " in w or ";" in w) and kw != "RUN" else [[w]]
                if kw == "RUN":
                    tokens = [words]
            else:
                tokens = shell_commands(args)
            runs(cur, tokens, kw, restricted=(kw == "RUN"))
        elif kw == "EXPOSE":
            cur["expose"] += [p for p in args.split() if re.fullmatch(r"[\w${}./-]{1,40}", p)]
        elif kw == "ENV":
            cur["env"] += _names(args, legacy_env=True)
        elif kw == "ARG":
            cur["args"] += _names(args)

    if global_args:
        df_node["build_args"] = list(dict.fromkeys(global_args))
    df_node["stages"] = len(stages)
    add(df_node)
    for i, st in enumerate(stages):
        node = st["node"]
        for key in ("expose", "env", "args"):
            if st[key]:
                node[key] = list(dict.fromkeys(st[key]))
        if i == len(stages) - 1:
            node["final"] = True
        add(node)
