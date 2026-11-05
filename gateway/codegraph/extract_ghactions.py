"""GitHub Actions extractor - the CI wiring a generic YAML or AST pass cannot see.

CI lives under the hidden `.github/` directory, which the shared prune rule (`skip_dir`) drops
from every walk. This pass therefore reads its two locations directly instead of walking for them,
and is switched off only when `.github` is named in `exclude`:

  <root>/.github/workflows/*.yml|*.yaml        workflows (GitHub reads that directory only, flat)
  <root>/.github/actions/**/action.yml|yaml    local actions (composite, node or docker)

plus any other local action directory a step reaches with `uses: ./<dir>`.

Emitted nodes:
  `ghworkflow:<rel>`                  a workflow file; label = its `name`, `triggers` = event names
  `ghjob:<rel>#<job>`                 a job; `runs_on` and `environment` (name only) attributes
  `ghaction:<dir>`                    a local action, keyed by the directory `uses: ./<dir>` names
  `extaction:<owner>/<repo>[/<path>]` a remote action (`docker://<image>` for container actions);
                                      the `@ref` is an edge attribute, not part of the id
  `extworkflow:<owner>/<repo>/<path>` a remote reusable workflow
  `ghsecret:<NAME>` / `ghvar:<NAME>`  a secret or configuration variable, by name only
  and, for files a `run:` line invokes, the id the pass that owns that file already uses -
  `module:<rel>` (python / tree-sitter languages), `playbook:<rel>` (Ansible),
  `dockerfile:<rel>` - or `file:<rel>` for a file no other pass graphs (an extensionless script,
  a `.pkr.json`). Those target nodes carry the same attributes their owning pass writes, so the
  graphs join whichever pass runs first, and the target still has a type without tree-sitter.

Emitted edges: contains (workflow -> job), depends_on (`needs`), uses_action (step -> remote or
local action), calls_workflow (job-level `uses:` -> `ghworkflow:` or `extworkflow:`),
uses_secret / uses_var (from `${{ secrets.X }}` / `${{ vars.X }}` and bare `if:` expressions),
and runs_script (INFERRED: job or action -> a local file its shell invokes - a path executed
directly, `bash|sh|pwsh|python|node|... <file>`, `uv|poetry|pipenv run <file>`,
`ansible-playbook <file>`, `docker|podman build [-f F] <ctx>`, `packer build|validate|init|fmt
<path>`, `terraform|tofu [-chdir=DIR]`, plus a node action's `main` and a docker action's
`Dockerfile`). Directories (packer, terraform) are linked through the files they hold. `cd`, step
`working-directory` and `defaults.run.working-directory` move the base a relative path resolves
against; `$GITHUB_WORKSPACE` and `github.action_path` are understood. A step whose shell is
pwsh/powershell (its `shell`, else `defaults.run.shell` of the job, then the workflow, else a
Windows runner's default) is tokenized without backslash escapes, so a Windows path such as
`.\\scripts\\build.ps1` resolves.

Deliberately absent: every VALUE. No `env:`/`with:` values, no input defaults, no matrix values,
no environment URLs, no container or service images, no secret contents (the graph holds secret
and variable NAMES and nothing else). Steps are not nodes - their edges hang off the job or
action that owns them - and no remote action or workflow is fetched. Nothing is evaluated: no
expression, no template, no network, no subprocess. A malformed, oversized or unreadable file is
skipped; a symlink that leaves the root is never read; output follows sorted file order. Each
file also has a work share proportional to its size, so YAML aliases that repeat the same steps
or text thousands of times cost that file its remaining edges, never unbounded time.
"""
from __future__ import annotations

import os
import posixpath
import re
import shlex
from pathlib import Path

import yaml

from .extract_ansible import skip_dir
from .treesitter import EXT_LANG

_FT = "github_actions"
_MAX_BYTES = 2 * 1024 * 1024    # a workflow bigger than this is generated or hostile: skip it
_MAX_NODES = 2000               # new nodes one file may add
_MAX_EDGES = 10_000             # edges one file may add
_MAX_ACTIONS = 500              # local action files parsed in total
_MAX_STEPS = 1000               # steps read per job or composite action
_MAX_DIR_FILES = 64             # files linked for one packer/terraform directory
_MAX_VISIT = 200_000            # values visited when scanning one document for secrets/vars
_WORK_PER_BYTE = 8              # work one file may spend (steps, run text, values scanned) ...
_WORK_BASE = 50_000             # ... is this plus _WORK_PER_BYTE x its size: aliases cannot
                                # multiply it
_SCORE = 0.8                    # runs_script is read out of shell text, so it is a heuristic
_ROOT = "@@ROOT@@"              # marker: what follows is relative to the repository root
_EXPR = "@@EXPR@@"              # an expression stood here; a path containing it is unknowable

_CTX_RE = re.compile(
    r"(?<![\w.])(secrets|vars)\s*(?:\.\s*([A-Za-z_][A-Za-z0-9_]*)"
    r"|\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\])")
_WORKSPACE_RE = re.compile(r"\$\{\{\s*github\.workspace\s*\}\}|\$\{GITHUB_WORKSPACE\}"
                           r"|\$GITHUB_WORKSPACE\b|\$env:GITHUB_WORKSPACE\b", re.I)
_ACTION_PATH_RE = re.compile(r"\$\{\{\s*github\.action_path\s*\}\}|\$\{GITHUB_ACTION_PATH\}"
                             r"|\$GITHUB_ACTION_PATH\b|\$env:GITHUB_ACTION_PATH\b", re.I)
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON_RE = re.compile(r"^(python|pypy)(\d+(\.\d+)?)?$")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_PUNCT = ";&|()<>"

# Leading words that do not name the program being run.
_PREFIX_WORDS = {"exec", "time", "nohup", "command", "builtin", "then", "do", "else", "elif",
                 "if", "while", "until", "!", "{", "}", "&"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "source", "."}
_PWSH = {"pwsh", "powershell"}
_INTERPRETERS = {"node", "ruby", "perl", "php", "Rscript", "deno", "bun", "tsx", "ts-node"}
_RUNNERS = {"uv", "poetry", "pipenv", "pdm", "hatch", "rye"}
# Options that consume the next word, per tool. A missing one only costs a missed edge: the
# option's argument is then taken for the file.
_ARG_OPTS = {
    "shell": {"-o", "+o", "-O", "+O"},
    "pwsh": {"-executionpolicy", "-ep", "-workingdirectory", "-wd", "-outputformat", "-of",
             "-inputformat", "-if", "-configurationname", "-settingsfile"},
    "python": {"-W", "-X", "-Q"},
    "runner": {"--with", "--with-requirements", "--with-editable", "--python", "-p", "--project",
               "--directory", "--package", "--extra", "--group", "--env-file", "--index",
               "--default-index", "--index-url", "--extra-index-url", "-e", "--env", "--only-group",
               "--no-group", "--config-file", "--cache-dir"},
    "ansible": {"-i", "--inventory", "--inventory-file", "-e", "--extra-vars", "-l", "--limit",
                "-t", "--tags", "--skip-tags", "-u", "--user", "--vault-id", "--vault-password-file",
                "--vault-pass-file", "-f", "--forks", "-c", "--connection", "-M", "--module-path",
                "--private-key", "--key-file", "-T", "--timeout", "--become-user",
                "--become-method", "--start-at-task", "--ssh-common-args", "--ssh-extra-args",
                "--scp-extra-args", "--sftp-extra-args"},
    "docker": {"-t", "--tag", "--build-arg", "--target", "--platform", "--label", "--cache-from",
               "--cache-to", "--secret", "--ssh", "-o", "--output", "--network", "--progress",
               "--iidfile", "--build-context", "--add-host", "--shm-size", "--ulimit", "-m",
               "--memory", "--memory-swap", "--cgroup-parent", "--metadata-file", "--provenance",
               "--sbom", "--attest", "--annotation", "--allow", "--builder", "--isolation",
               "--cpu-shares", "--cpuset-cpus", "--security-opt", "--squash-all", "--layers",
               "--format", "--authfile", "--creds", "--volume", "-v", "--env", "--os", "--arch",
               "--variant", "--jobs", "--timestamp", "--manifest"},
    "packer": {"-var", "-var-file", "-only", "-except", "-parallel-builds", "-on-error"},
}


class _Full(Exception):
    """The current file has spent its node or edge budget; keep what it produced."""


def _triggers(data: dict) -> list[str]:
    # PyYAML reads the bare key `on` as the boolean True (YAML 1.1), so look for both.
    on = data["on"] if "on" in data else data.get(True)
    if isinstance(on, str):
        return [on]
    if isinstance(on, list):
        return [x for x in on if isinstance(x, str)]
    if isinstance(on, dict):
        return [k for k in on if isinstance(k, str)]
    return []


def _labels(v):
    """A label or a list of labels; nothing else (never a mapping, so never recursive)."""
    if isinstance(v, (str, int)) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, list):
        return [str(x) for x in v if isinstance(x, (str, int)) and not isinstance(x, bool)] or None
    return None


def _runs_on(v):
    """Runner labels: a label, a list of labels, or a {group, labels} mapping. Not recursive:
    a YAML alias can make `labels` the mapping itself."""
    if isinstance(v, dict):
        out = {}
        if isinstance(v.get("group"), str):
            out["group"] = v["group"]
        labels = _labels(v.get("labels"))
        if labels:
            out["labels"] = labels
        return out or None
    return _labels(v)


def _shell(*objs) -> str | None:
    """The first explicit shell: a step's `shell`, then `defaults.run.shell` of its job and
    workflow (pass them innermost first)."""
    for o in objs:
        if not isinstance(o, dict):
            continue
        s = o.get("shell")
        if s is None:
            d = o.get("defaults")
            run = d.get("run") if isinstance(d, dict) else None
            s = run.get("shell") if isinstance(run, dict) else None
        if isinstance(s, str) and s.strip():
            return s.strip()
    return None


def _is_pwsh(shell: str | None, runs_on=None) -> bool:
    """PowerShell syntax (no backslash escapes): `shell: pwsh|powershell`, or no shell at all on
    a Windows runner, where GitHub's default is pwsh."""
    if shell is not None:
        return shell.split()[0].lower() in ("pwsh", "powershell")
    labels = runs_on.get("labels") if isinstance(runs_on, dict) else runs_on
    labels = [labels] if isinstance(labels, str) else labels if isinstance(labels, list) else []
    return any("windows" in str(x).lower() for x in labels)


def _expr_spans(s: str) -> list[tuple[int, int, int, int]]:
    """(start, end, inner start, inner end) of every `${{ ... }}`, the first `}}` closing each.
    A linear scan: an unclosed `${{` ends the search for good, where the lazy regex this
    replaces rescanned the rest of the string from every later `${{` (quadratic)."""
    out, pos = [], 0
    while True:
        a = s.find("${{", pos)
        if a < 0:
            break
        b = s.find("}}", a + 3)
        if b < 0:
            break
        out.append((a, b + 2, a + 3, b))
        pos = b + 2
    return out


def _expr_sub(s: str, repl: str) -> str:
    spans = _expr_spans(s)
    if not spans:
        return s
    parts, pos = [], 0
    for a, b, _, _ in spans:
        parts += [s[pos:a], repl]
        pos = b
    parts.append(s[pos:])
    return "".join(parts)


def _environment(v):
    """The environment NAME only - its `url` is a value."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and isinstance(v.get("name"), str):
        return v["name"]
    return None


def _wd(obj) -> str | None:
    d = obj.get("defaults") if isinstance(obj, dict) else None
    run = d.get("run") if isinstance(d, dict) else None
    wd = run.get("working-directory") if isinstance(run, dict) else None
    return wd if isinstance(wd, str) else None


def _ctx_names(obj, limit: int = _MAX_VISIT, cost: list | None = None) -> list[tuple[str, str]]:
    """(secrets|vars, NAME) pairs referenced anywhere in `obj`, in first-seen order.

    Only expression text is scanned: `${{ ... }}` spans in any string, and the whole of an `if:`
    value (GitHub evaluates it as an expression even without the braces). Iterative, bounded,
    and alias-safe: a YAML alias bomb shares objects, so each container is visited once.
    `cost`, when given, receives the number of values visited plus the characters scanned."""
    found: dict[tuple[str, str], None] = {}
    stack = [(obj, False)]
    seen: set[int] = set()
    visited = 0
    limit = min(limit, _MAX_VISIT)
    while stack and visited < limit:
        visited += 1
        o, is_if = stack.pop()
        if isinstance(o, str):
            visited += len(o) // 64      # long strings cost in proportion to their length
            spans = [o[a:b] for _, _, a, b in _expr_spans(o)]
            if is_if and not spans:
                spans = [o]
            for s in spans:
                for m in _CTX_RE.finditer(s):
                    found.setdefault((m.group(1), m.group(2) or m.group(3)), None)
        elif isinstance(o, (dict, list)):
            if id(o) in seen:
                continue
            seen.add(id(o))
            items = list(o.items()) if isinstance(o, dict) else [(None, v) for v in o]
            for k, v in reversed(items):
                stack.append((v, k == "if"))
    if cost is not None:
        cost.append(visited)
    return list(found)


def _logical_lines(script: str) -> list[str]:
    """Shell text -> logical lines: continuations joined, comments and heredoc bodies dropped."""
    script = re.sub(r"\\\r?\n", " ", script)
    script = re.sub(r"`\r?\n", " ", script)  # PowerShell continuation
    out: list[str] = []
    heredoc = None
    for line in script.splitlines():
        s = line.strip()
        if heredoc is not None:
            if s == heredoc:
                heredoc = None
            continue
        if not s or s.startswith("#"):
            continue
        m = _HEREDOC_RE.search(s)
        if m:
            heredoc = m.group(2)
        out.append(s)
    return out


def _tokens(line: str, ps: bool = False) -> list[str]:
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=_PUNCT)
        lex.whitespace_split = True
        if ps:              # PowerShell has no backslash escape: `.\x\y.ps1` is a path
            lex.escape = ""
        return list(lex)
    except ValueError:  # unbalanced quote: fall back to a plain split, never fail
        return re.findall(r"[;&|()<>]+|[^\s;&|()<>]+", line)


def _commands(line: str, ps: bool = False) -> list[list[str]]:
    """One logical line -> simple commands, split at ; && || | & ( ), redirect targets dropped."""
    cmds: list[list[str]] = [[]]
    skip = False
    for t in _tokens(line, ps):
        if t and all(c in _PUNCT for c in t):
            if "<" in t or ">" in t:
                skip = not t.endswith("&")  # `> f` names a file to drop; `2>&1` names a fd
                continue
            cmds.append([])
            continue
        if skip:
            skip = False
            continue
        cmds[-1].append(t)
    return [c for c in cmds if c]


def _strip_prefix(cmd: list[str]) -> list[str]:
    i = 0
    while i < len(cmd):
        t = cmd[i]
        if _ASSIGN_RE.match(t) or t in _PREFIX_WORDS:
            i += 1
        elif t == "sudo":
            i += 1
            while i < len(cmd) and cmd[i].startswith("-"):
                i += 2 if cmd[i] in ("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U") else 1
        elif t == "env":
            i += 1
            while i < len(cmd) and (cmd[i].startswith("-") or _ASSIGN_RE.match(cmd[i])):
                i += 2 if cmd[i] in ("-u", "-C", "-S") else 1
        else:
            break
    return cmd[i:]


def _positionals(args: list[str], arg_opts: set[str], lower: bool = False) -> list[str]:
    out, i = [], 0
    while i < len(args):
        a = args[i]
        key = a.lower() if lower else a
        if a == "--":
            out.extend(args[i + 1:])
            break
        if a.startswith("-") and len(a) > 1:
            i += 2 if (key in arg_opts and "=" not in a) else 1
            continue
        out.append(a)
        i += 1
    return out


def _option(args: list[str], names: tuple[str, ...]) -> str | None:
    """Value of the first of `names` (`-f X`, `--file X`, `--file=X`)."""
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                return args[i + 1]
            if n.startswith("--") and a.startswith(n + "="):
                return a[len(n) + 1:]
    return None


def _is_path(word: str) -> bool:
    return "/" in word or word.startswith(".\\") or word.startswith(_ROOT)


def _targets(cmd: list[str], depth: int = 0, ps: bool = False) -> list[tuple[str, str]]:
    """What a simple command runs, as (kind, path-relative-to-cwd) pairs. Kinds: script,
    playbook, dockerfile, dockerctx, packer, tfdir, plus cd (a directory change the caller
    applies to what follows) and push/pop around a nested shell. `ps`: the line was tokenized
    as PowerShell. Nothing here touches the filesystem."""
    cmd = _strip_prefix(cmd)
    if not cmd or depth > 3:
        return []
    word, args = cmd[0], cmd[1:]
    name = posixpath.basename(word.replace("\\", "/"))
    if name.lower().endswith(".exe"):
        name = name[:-4]

    def nested(text: str, child_ps: bool):  # `bash -c "..."`, `pwsh -Command "..."`: a child
        out = [("push", "")]  # process, so a `cd` inside it must not move the caller - push/pop
        for line in _logical_lines(text):
            for c in _commands(line, child_ps):
                out += _targets(c, depth + 1, child_ps)
        return out + [("pop", "")]

    if word in ("cd", "pushd", "popd", "Set-Location", "sl", "Push-Location", "Pop-Location"):
        if word in ("popd", "Pop-Location"):
            return [("cd", "")]  # back to the start: the stack itself is not tracked
        return [("cd", next((a for a in args if not a.startswith("-")), ""))]
    if name in _SHELLS and (word == name or (word.startswith("/") and name != ".")):
        if "-c" in args:
            i = args.index("-c")
            return nested(args[i + 1], False) if i + 1 < len(args) else []
        pos = _positionals(args, _ARG_OPTS["shell"])
        return [("script", pos[0])] if pos else []
    if name.lower() in _PWSH:
        for i, a in enumerate(args):
            al = a.lower()
            if al in ("-file", "-f") and i + 1 < len(args):
                return [("script", args[i + 1])]
            if al in ("-command", "-c") and i + 1 < len(args):
                return nested(" ".join(args[i + 1:]), True)
        pos = _positionals(args, _ARG_OPTS["pwsh"], lower=True)
        if not pos:
            return []
        return [("script", pos[0])] if pos[0].lower().endswith(".ps1") else nested(" ".join(pos), True)
    if _PYTHON_RE.match(name) or name == "py":
        if any(a in ("-m", "-c") for a in args):
            return []
        pos = _positionals(args, _ARG_OPTS["python"])
        return [("script", pos[0])] if pos else []
    if name in _INTERPRETERS:
        pos = _positionals(args, set())
        if pos and pos[0] == "run" and name in ("deno", "bun"):
            pos = pos[1:]
        return [("script", pos[0])] if pos else []
    if name in _RUNNERS and args and args[0] == "run":
        rest = args[1:]
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if (rest[i] in _ARG_OPTS["runner"] and "=" not in rest[i]) else 1
        rest = rest[i:]
        if not rest:
            return []
        inner = _targets(rest, depth + 1, ps)
        if inner:
            return inner
        tail = posixpath.splitext(rest[0])[1].lower()
        return [("script", rest[0])] if (tail == ".py" or tail in EXT_LANG) else []
    if name == "ansible-playbook":
        return [("playbook", p) for p in _positionals(args, _ARG_OPTS["ansible"])
                if p.lower().endswith((".yml", ".yaml"))]
    if name in ("docker", "podman", "buildah", "nerdctl"):
        sub = [a for a in args if not a.startswith("-")]
        if not sub:
            return []
        if sub[0] in ("build", "bud"):
            rest = args[args.index(sub[0]) + 1:]
        elif sub[0] in ("buildx", "image", "builder") and len(sub) > 1 and sub[1] == "build":
            rest = args[args.index(sub[1]) + 1:]
        else:
            return []
        f = _option(rest, ("-f", "--file"))
        if f and f != "-":
            return [("dockerfile", f)]
        pos = _positionals(rest, _ARG_OPTS["docker"] | {"-f", "--file"})
        return [("dockerctx", pos[-1])] if pos and pos[-1] != "-" and "://" not in pos[-1] else []
    if name == "packer":
        if not args or args[0] not in ("build", "validate", "init", "fmt", "inspect"):
            return []
        pos = _positionals(args[1:], _ARG_OPTS["packer"])
        kind = "packer_r" if any(a.lstrip("-") == "recursive" for a in args[1:]) else "packer"
        return [(kind, pos[-1])] if pos else []
    if name in ("terraform", "tofu"):
        chdir = next((a.split("=", 1)[1] for a in args if a.startswith("-chdir=")), None)
        sub = [a for a in args if not a.startswith("-")]
        return [("tfdir", chdir or ".")] if sub else []
    if _is_path(word):
        return [("script", word)]
    return []


class _Pass:
    def __init__(self, root: Path, exclude: frozenset[str], include: frozenset[str]):
        self.root = root
        self.base = root.resolve()
        self.exclude = exclude
        self.include = include
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._edge_at: dict[tuple, dict] = {}
        self._nodes_left = self._edges_left = self._work_left = 0
        self.actions: list[str] = []          # local action directories, in discovery order
        self._actions_seen: set[str] = set()
        self.source = ""                      # repo-relative file being read

    # -- graph ---------------------------------------------------------------------------
    def budget(self):
        self._nodes_left, self._edges_left = _MAX_NODES, _MAX_EDGES
        self._work_left = _WORK_BASE

    def work_for(self, text: str):
        """The file's work share grows with its size (see spend)."""
        self._work_left = _WORK_PER_BYTE * len(text) + _WORK_BASE

    def node(self, nid: str, **attrs) -> str:
        if nid not in self.nodes:
            if self._nodes_left <= 0:
                raise _Full
            self._nodes_left -= 1
            self.nodes[nid] = {"id": nid, **{k: v for k, v in attrs.items() if v is not None}}
        return nid

    def edge(self, src: str, dst: str, rel: str, confidence: str = "EXTRACTED", **attrs):
        key = (src, dst, rel)
        cur = self._edge_at.get(key)
        if cur is not None:  # one edge per pair; a second ref is recorded, not a second edge
            ref = attrs.get("ref")
            if ref and cur.get("ref") and ref != cur["ref"]:
                cur["refs"] = sorted(set(cur.get("refs", [cur["ref"]])) | {ref})
            return
        if self._edges_left <= 0:
            raise _Full
        self._edges_left -= 1
        e = {"source": src, "target": dst, "relation": rel, "confidence": confidence,
             **{k: v for k, v in attrs.items() if v is not None}}
        self._edge_at[key] = e
        self.edges.append(e)

    def ctx_edges(self, owner: str, obj):
        cost: list[int] = []
        names = _ctx_names(obj, limit=max(1, self._work_left), cost=cost)
        self.spend(sum(cost))
        for kind, name in names:
            if kind == "secrets":
                nid = self.node(f"ghsecret:{name}", label=name, type="secret", file_type=_FT,
                                source_file=self.source)
                self.edge(owner, nid, "uses_secret")
            else:
                nid = self.node(f"ghvar:{name}", label=name, type="variable", file_type=_FT,
                                source_file=self.source)
                self.edge(owner, nid, "uses_var")

    def file_node(self, rel: str, kind: str = "script") -> str:
        """The node another pass already uses for this file, with that pass's attributes."""
        base = posixpath.basename(rel)
        if kind == "playbook":
            return self.node(f"playbook:{rel}", label=base, type="playbook", file_type="ansible",
                             source_file=rel)
        if kind == "dockerfile":
            return self.node(f"dockerfile:{rel}", label=base, type="dockerfile",
                             file_type="docker", source_file=rel)
        suffix = posixpath.splitext(rel)[1]
        if suffix == ".py":
            return self.node(f"module:{rel}", label=rel, type="module", file_type="python",
                             source_file=rel)
        lang = EXT_LANG.get(suffix.lower())
        if lang:
            return self.node(f"module:{rel}", label=rel, type="module", file_type=lang,
                             source_file=rel)
        # same shape extract_docker gives file:<rel>, so the node merges cleanly in either order
        return self.node(f"file:{rel}", label=base, type="file", file_type="file",
                         source_file=rel)

    # -- filesystem ----------------------------------------------------------------------
    def contained(self, p: Path) -> bool:
        try:
            p.resolve().relative_to(self.base)
            return True
        except (OSError, ValueError, RuntimeError):
            return False

    def path(self, rel: str) -> Path:
        return self.root if rel in ("", ".") else self.root / rel

    def is_file(self, rel: str) -> bool:
        p = self.path(rel)
        try:
            return self.contained(p) and p.is_file()
        except OSError:
            return False

    def is_dir(self, rel: str) -> bool:
        p = self.path(rel)
        try:
            return self.contained(p) and p.is_dir()
        except OSError:
            return False

    def read_yaml(self, rel: str):
        p = self.path(rel)
        try:
            if not self.contained(p) or not p.is_file() or p.stat().st_size > _MAX_BYTES:
                return None, ""
            text = p.read_text(encoding="utf-8", errors="replace")
            return yaml.safe_load(text), text
        except Exception:  # malformed or unreadable: skip the file, never the build
            return None, ""

    def resolve(self, cwd: str | None, word: str) -> str | None:
        """A word from a shell line -> a repo-relative POSIX path, lexically; None if unknowable
        or outside the root."""
        if not word or _EXPR in word or "$" in word or any(c in word for c in "*?[~`"):
            return None
        word = word.replace("\\", "/")
        if word.startswith(_ROOT):
            joined = posixpath.normpath(word[len(_ROOT):].lstrip("/") or ".")
        else:
            if cwd is None or word.startswith("/"):
                return None
            joined = posixpath.normpath(posixpath.join(cwd, word))
        if joined == ".." or joined.startswith("../") or joined.startswith("/"):
            return None
        if joined != "." and any(part in self.exclude for part in joined.split("/")):
            return None
        return joined

    def dir_files(self, rel: str, suffixes: tuple[str, ...], recursive: bool = False) -> list[str]:
        """Files in `rel` ending in `suffixes`, sorted and capped; `recursive` walks below it
        with the shared prune rule and never follows a directory link."""
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.path(rel)):
            dirnames[:] = sorted(d for d in dirnames
                                 if recursive and not skip_dir(d, self.exclude, self.include))
            drel = Path(dirpath).relative_to(self.root).as_posix()
            drel = "" if drel == "." else drel
            for n in sorted(filenames):
                r = _join(drel, n)
                if n.endswith(suffixes) and self.is_file(r):
                    out.append(r)
                    if len(out) >= _MAX_DIR_FILES:
                        return out
        return out

    def norm_dir(self, wd: str | None, base: str | None = "") -> str | None:
        """A working-directory value -> repo-relative dir ('' = root); None if unknowable."""
        if wd is None:
            return base
        wd = _expr_sub(_WORKSPACE_RE.sub(_ROOT, wd), _EXPR).strip()
        r = self.resolve("", wd)
        return None if r is None else ("" if r == "." else r)

    # -- github actions ------------------------------------------------------------------
    def local_action(self, word: str) -> str | None:
        """`./dir` -> 'dir' when an action.yml/yaml is there; queued for parsing."""
        d = self.resolve("", word)
        if d is None:
            return None
        if not any(self.is_file(_join(d, f)) for f in ("action.yml", "action.yaml")):
            return None
        if d not in self._actions_seen:
            self._actions_seen.add(d)
            self.actions.append(d)
        return d

    def uses_action(self, owner: str, uses: str):
        u = uses.strip()
        if u.startswith("./") or u == ".":
            d = self.local_action(u)
            if d is not None:
                self.edge(owner, f"ghaction:{d}", "uses_action")
            return
        if u.startswith("docker://"):
            img, ref = u[len("docker://"):], None
            if "@" in img:
                img, ref = img.split("@", 1)
            elif ":" in img.rsplit("/", 1)[-1]:
                img, ref = img.rsplit(":", 1)
            if img:
                nid = self.node(f"extaction:docker://{img}", label=f"docker://{img}",
                                type="external_action", file_type=_FT, source_file=self.source)
                self.edge(owner, nid, "uses_action", ref=ref)
            return
        name, _, ref = u.partition("@")
        parts = [p for p in name.split("/") if p]
        if len(parts) < 2 or _EXPR in u or "${{" in u:
            return
        ident = "/".join([parts[0].lower(), parts[1].lower(), *parts[2:]])
        nid = self.node(f"extaction:{ident}", label=ident, type="external_action", file_type=_FT,
                        source_file=self.source)
        self.edge(owner, nid, "uses_action", ref=ref or None)

    def calls_workflow(self, owner: str, uses: str, job: dict):
        u = uses.strip()
        inherit = True if job.get("secrets") == "inherit" else None
        if u.startswith("./"):
            rel = self.resolve("", u)
            if rel is not None and self.is_file(rel):
                self.edge(owner, f"ghworkflow:{rel}", "calls_workflow", inherit_secrets=inherit)
            return
        name, _, ref = u.partition("@")
        parts = [p for p in name.split("/") if p]
        if len(parts) < 3 or "${{" in u:
            return
        ident = "/".join([parts[0].lower(), parts[1].lower(), *parts[2:]])
        nid = self.node(f"extworkflow:{ident}", label=ident, type="external_workflow",
                        file_type=_FT, source_file=self.source)
        self.edge(owner, nid, "calls_workflow", ref=ref or None, inherit_secrets=inherit)

    def link(self, owner: str, kind: str, cwd: str | None, word: str):
        rel = self.resolve(cwd, word)
        if rel is None:
            return
        targets: list[tuple[str, str]] = []
        if kind == "script" and self.is_file(rel):
            targets.append((rel, "script"))
        elif kind == "playbook" and self.is_file(rel):
            targets.append((rel, "playbook"))
        elif kind == "dockerfile" and self.is_file(rel):
            targets.append((rel, "dockerfile"))
        elif kind == "dockerctx" and self.is_dir(rel):
            for n in ("Dockerfile", "Containerfile"):
                f = _join(rel, n)
                if self.is_file(f):
                    targets.append((f, "dockerfile"))
                    break
        elif kind in ("packer", "packer_r"):
            if self.is_dir(rel):
                targets += [(f, "script") for f in self.dir_files(rel, (".pkr.hcl", ".pkr.json"),
                                                                 recursive=kind == "packer_r")]
            elif self.is_file(rel):
                targets.append((rel, "script"))
        elif kind == "tfdir" and self.is_dir(rel):
            targets += [(f, "script") for f in self.dir_files(rel, (".tf", ".tf.json"))]
        for f, fkind in targets:
            self.edge(owner, self.file_node(f, fkind), "runs_script", "INFERRED",
                      confidence_score=_SCORE)

    def spend(self, units: int):
        """Charge work to the current file. YAML aliases let a few kilobytes name the same
        steps, `run` text or env map thousands of times, so the node and edge caps alone do not
        bound the time a file takes; a file that exhausts its share keeps what it produced."""
        self._work_left -= units
        if self._work_left < 0:
            raise _Full

    def run(self, owner: str, script: str, cwd: str | None, action_dir: str | None = None,
            ps: bool = False):
        self.spend(len(script) + 1)
        s = _WORKSPACE_RE.sub(_ROOT, script)
        if action_dir is not None:
            s = _ACTION_PATH_RE.sub(_ROOT + ("" if action_dir in ("", ".") else "/" + action_dir), s)
        s = _expr_sub(s, _EXPR)
        start = cwd
        scopes: list[str | None] = []
        for line in _logical_lines(s):
            for cmd in _commands(line, ps):
                for kind, word in _targets(cmd, 0, ps):
                    if kind == "push":
                        scopes.append(cwd)
                    elif kind == "pop":
                        cwd = scopes.pop() if scopes else start
                    elif kind != "cd":
                        self.link(owner, kind, cwd, word)
                    elif not word or word == "-":
                        cwd = start
                    else:
                        r = self.resolve(cwd, word)
                        cwd = None if r is None else ("" if r == "." else r)

    def steps(self, owner: str, steps, cwd: str | None, action_dir: str | None = None,
              scopes: tuple = (), runs_on=None):
        """`scopes`: the job and workflow (innermost first) whose `defaults.run.shell` applies."""
        seen: set[tuple] = set()
        for step in (steps if isinstance(steps, list) else [])[:_MAX_STEPS]:
            self.spend(1)
            if not isinstance(step, dict):
                continue
            wd = step.get("working-directory")
            scwd = self.norm_dir(wd) if isinstance(wd, str) else cwd
            key = (id(step), scwd)
            if key in seen:      # the same (aliased) step again: same edges, nothing new
                continue
            seen.add(key)
            if isinstance(step.get("uses"), str):
                self.uses_action(owner, step["uses"])
            if isinstance(step.get("run"), str):
                ps = _is_pwsh(_shell(step, *scopes), runs_on)
                self.run(owner, step["run"], scwd, action_dir, ps)

    def workflow(self, rel: str):
        data, text = self.read_yaml(rel)
        if not isinstance(data, dict):
            return
        self.source = rel
        self.work_for(text)
        name = data.get("name")
        wid = self.node(f"ghworkflow:{rel}", label=name if isinstance(name, str) else
                        posixpath.basename(rel), type="workflow", file_type=_FT, source_file=rel,
                        triggers=_triggers(data))
        self.ctx_edges(wid, {k: v for k, v in data.items() if k not in ("jobs", "on", True)})
        wf_cwd = self.norm_dir(_wd(data))
        jobs = data.get("jobs")
        if not isinstance(jobs, dict):
            return
        lines = text.splitlines()
        valid = {j: job for j, job in jobs.items() if isinstance(j, str) and isinstance(job, dict)}
        for j, job in valid.items():
            label = job.get("name")
            jid = self.node(f"ghjob:{rel}#{j}", label=label if isinstance(label, str) else j,
                            type="job", file_type=_FT, source_file=rel,
                            source_location=_job_line(lines, j), runs_on=_runs_on(job.get("runs-on")),
                            environment=_environment(job.get("environment")))
            self.edge(wid, jid, "contains")
        for j, job in valid.items():
            jid = f"ghjob:{rel}#{j}"
            needs = job.get("needs")
            for n in [needs] if isinstance(needs, str) else needs if isinstance(needs, list) else []:
                if isinstance(n, str) and n in valid:
                    self.edge(jid, f"ghjob:{rel}#{n}", "depends_on")
            if isinstance(job.get("uses"), str):
                self.calls_workflow(jid, job["uses"], job)
            self.ctx_edges(jid, job)
            jd = _wd(job)
            cwd = self.norm_dir(jd) if jd is not None else wf_cwd
            self.steps(jid, job.get("steps"), cwd, scopes=(job, data),
                       runs_on=_runs_on(job.get("runs-on")))

    def action(self, d: str):
        frel = next((r for r in (_join(d, "action.yml"), _join(d, "action.yaml"))
                     if self.is_file(r)), None)
        if frel is None:
            return
        data, text = self.read_yaml(frel)
        if not isinstance(data, dict):
            return
        self.source = frel
        self.work_for(text)
        runs = data.get("runs") if isinstance(data.get("runs"), dict) else {}
        using = runs.get("using") if isinstance(runs.get("using"), str) else None
        name = data.get("name")
        aid = self.node(f"ghaction:{d}", label=name if isinstance(name, str) else
                        posixpath.basename(d) or d, type="action", file_type=_FT,
                        source_file=frel, using=using)
        self.ctx_edges(aid, data)
        kind = (using or "").lower()
        adir = "" if d == "." else d
        if kind == "composite":
            self.steps(aid, runs.get("steps"), "", action_dir=adir)
        elif kind.startswith("node"):
            for k in ("pre", "main", "post"):
                if isinstance(runs.get(k), str):
                    self.link(aid, "script", adir, runs[k])
        elif kind == "docker" and isinstance(runs.get("image"), str):
            image = runs["image"].strip()
            if image.startswith("docker://"):
                self.uses_action(aid, image)
            else:
                self.link(aid, "dockerfile", adir, image)


def _join(d: str, name: str) -> str:
    return name if d in ("", ".") else f"{d}/{name}"


def _job_line(lines: list[str], job: str) -> str | None:
    """`L<n>` of a job's key under the top-level `jobs:` mapping (PyYAML keeps no positions)."""
    pat = re.compile(r"^\s+['\"]?" + re.escape(job) + r"['\"]?\s*:")
    in_jobs = False
    for i, line in enumerate(lines):
        if re.match(r"^jobs\s*:", line):
            in_jobs = True
        elif in_jobs and re.match(r"^\S", line) and not line.startswith("#"):
            in_jobs = False
        elif in_jobs and pat.match(line):
            return f"L{i + 1}"
    return None


def extract(root: Path, exclude: frozenset[str] = frozenset(),
            include: frozenset[str] = frozenset()) -> dict:
    root = Path(root)
    if ".github" in exclude:
        return {"nodes": [], "edges": []}
    try:
        p = _Pass(root, frozenset(exclude), frozenset(include))
    except OSError:
        return {"nodes": [], "edges": []}
    gh = root / ".github"
    if not p.is_dir(".github"):  # absent, or a symlink that leaves the root
        return {"nodes": [], "edges": []}

    if "workflows" not in exclude and p.is_dir(".github/workflows"):
        try:
            names = sorted(os.listdir(gh / "workflows"))
        except OSError:
            names = []
        for n in names:
            rel = f".github/workflows/{n}"
            if n.lower().endswith((".yml", ".yaml")) and p.is_file(rel):
                p.budget()
                try:
                    p.workflow(rel)
                except _Full:
                    pass
                except Exception:   # a shape nothing here foresaw costs this file, never the build
                    pass

    # Local actions under .github/actions: a pruned, sorted walk that never follows a link out.
    if "actions" not in exclude and p.is_dir(".github/actions"):
        found = []
        for dirpath, dirnames, filenames in os.walk(gh / "actions"):
            dirnames[:] = sorted(x for x in dirnames if not skip_dir(x, exclude, include))
            if "action.yml" in filenames or "action.yaml" in filenames:
                found.append(Path(dirpath).relative_to(root).as_posix())
        for d in sorted(found):
            p.local_action("./" + d)

    # Referenced local actions are appended while earlier ones are read; the list is the queue.
    i = 0
    while i < len(p.actions) and i < _MAX_ACTIONS:
        p.budget()
        try:
            p.action(p.actions[i])
        except _Full:
            pass
        except Exception:
            pass
        i += 1

    return {"nodes": list(p.nodes.values()), "edges": p.edges}
