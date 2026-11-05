#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Compare a vault's team.txt and repos.txt with its GitHub team (stdlib and the gh CLI; read-only).

Usage, from the vault repository root:

    python3 scripts/roster_check.py                       # Windows: py -3 scripts\\roster_check.py
    python3 scripts/roster_check.py --team <org>/<team-slug> --json

The team is `--team`, else the "# github-team: <org>/<team-slug>" line of team.txt. Reads the members and
repositories of that GitHub team with `gh api` (sign in with `gh auth login`, or set GH_TOKEN) and reports: members
missing from team.txt, logins in team.txt that are no longer in the team, repositories of the team missing from
repos.txt, entries of repos.txt the team no longer has, archived repositories and default branches that differ. For
each difference it names the files to update; the `vault-roster` skill describes the procedure. The vault repository
itself is ignored. Exit status: 0 in sync, 1 when something differs, 2 on a usage error or when GitHub cannot be read.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEAM_RE = re.compile(r"^#\s*github-team:\s*([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)\s*$")

FILES_MEMBER_ADDED = ["team.txt", "wiki/hot.<login>.md", "wiki/team/<login>.md", "the vault's team roster page"]
FILES_MEMBER_REMOVED = ["team.txt", "wiki/hot.<login>.md", "wiki/team/<login>.md", "the vault's team roster page",
                        "pages that name <login> as an owner"]
FILES_REPO_ADDED = ["repos.txt", ".claude/settings.json (claudeMdExcludes, if the vault has it)",
                    "the vault's repository catalog", "then wiki/sources/<repo>/ and wiki/.graph/<repo>.json"]
FILES_REPO_REMOVED = ["repos.txt", ".claude/settings.json (claudeMdExcludes, if the vault has it)",
                      "the vault's repository catalog", "wiki/sources/<repo>/ and wiki/.graph/<repo>.*"]


def gh_json(path: str) -> list:
    """All pages of a GitHub REST list endpoint, as one list."""
    gh = shutil.which("gh")
    if gh is None:
        raise RuntimeError("the gh CLI is not on PATH")
    # `--jq .[]` prints one compact JSON item per line across all pages (works without gh's newer --slurp).
    proc = subprocess.run([gh, "api", "--paginate", path, "--jq", ".[]"], capture_output=True)
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"gh api {path}: {message}")
    return [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]


def read_list(path: Path) -> tuple[dict[str, str], tuple[str, str] | None]:
    """{first field: second field} of a roster file, and the github-team directive if the file has one."""
    entries: dict[str, str] = {}
    team = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = TEAM_RE.match(raw.strip())
        if match and team is None:
            team = (match.group(1), match.group(2))
        fields = raw.split("#", 1)[0].split()
        if fields:
            entries[fields[0]] = fields[1] if len(fields) > 1 else ""
    return entries, team


def own_repository() -> str | None:
    proc = subprocess.run(["git", "-C", str(ROOT), "remote", "get-url", "origin"], capture_output=True, text=True)
    match = re.search(r"([^/:]+?)(?:\.git)?/?$", proc.stdout.strip()) if proc.returncode == 0 else None
    return match.group(1) if match else None


def compare(team_txt: dict[str, str], repos_txt: dict[str, str], members: set[str], repos: dict[str, dict]) -> list:
    lower_members = {m.lower(): m for m in members}
    diffs: list[dict] = []
    for login in sorted(members - set(team_txt), key=str.lower):
        listed = next((t for t in team_txt if t.lower() == login.lower()), None)
        if listed:
            diffs.append({"kind": "member-case", "name": login,
                          "detail": f"team.txt lists {listed!r}; GitHub spells it {login!r}", "files": FILES_MEMBER_ADDED})
        else:
            diffs.append({"kind": "member-added", "name": login, "detail": "in the GitHub team, not in team.txt",
                          "files": FILES_MEMBER_ADDED})
    for login in sorted(set(team_txt) - members, key=str.lower):
        if login.lower() not in lower_members:
            diffs.append({"kind": "member-removed", "name": login, "detail": "in team.txt, no longer in the GitHub team",
                          "files": FILES_MEMBER_REMOVED})
    for name in sorted(set(repos) - set(repos_txt)):
        diffs.append({"kind": "repo-added", "name": name, "detail": "the team has it, repos.txt does not",
                      "files": FILES_REPO_ADDED})
    for name in sorted(set(repos_txt) - set(repos)):
        diffs.append({"kind": "repo-removed", "name": name,
                      "detail": "in repos.txt, but the team no longer has it (removed, renamed or transferred)",
                      "files": FILES_REPO_REMOVED})
    for name in sorted(set(repos) & set(repos_txt)):
        repo = repos[name]
        if repo.get("archived"):
            diffs.append({"kind": "repo-archived", "name": name, "detail": "archived on GitHub",
                          "files": ["wiki/sources/<repo>/ (note the archive)", "the vault's repository catalog"]})
        if repo.get("default_branch") and repo["default_branch"] != repos_txt[name]:
            diffs.append({"kind": "repo-default-branch", "name": name,
                          "detail": f"repos.txt says {repos_txt[name]!r}, GitHub says {repo['default_branch']!r}",
                          "files": ["repos.txt", "the vault's repository catalog", "then a re-scan of the repository"]})
    return diffs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare team.txt and repos.txt with the vault's GitHub team.")
    parser.add_argument("--team", metavar="ORG/SLUG", help="GitHub team (default: the '# github-team:' line of team.txt)")
    parser.add_argument("--ignore", action="append", default=[], help="repository to ignore (repeatable)")
    parser.add_argument("--json", action="store_true", help="print the differences as JSON")
    args = parser.parse_args(argv)

    for name in ("team.txt", "repos.txt"):
        if not (ROOT / name).is_file():
            print(f"roster_check: {name} not found (copy {name.replace('.txt', '.example.txt')})", file=sys.stderr)
            return 2
    team_txt, team_line = read_list(ROOT / "team.txt")
    repos_txt, _ = read_list(ROOT / "repos.txt")
    if args.team:
        org, _, slug = args.team.partition("/")
        team = (org, slug) if org and slug else None
    else:
        team = team_line
    if team is None:
        print("roster_check: no team; pass --team <org>/<slug> or add '# github-team: <org>/<slug>' to team.txt",
              file=sys.stderr)
        return 2
    org, slug = team
    ignore = set(args.ignore) | ({own_repository()} - {None})
    try:
        members = {m["login"] for m in gh_json(f"orgs/{org}/teams/{slug}/members")}
        repos = {r["name"]: r for r in gh_json(f"orgs/{org}/teams/{slug}/repos") if r["name"] not in ignore}
    except (RuntimeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"roster_check: cannot read the team {org}/{slug}: {exc}", file=sys.stderr)
        return 2

    diffs = compare(team_txt, repos_txt, members, repos)
    if args.json:
        print(json.dumps({"org": org, "team": slug, "in_sync": not diffs, "differences": diffs}, indent=2))
    else:
        print(f"roster_check: {org}/{slug}: {len(members)} members, {len(repos)} repositories "
              f"(team.txt {len(team_txt)}, repos.txt {len(repos_txt)})")
        for d in diffs:
            print(f"  {d['kind']:<20} {d['name']}: {d['detail']}")
            print(f"  {'':<20} update: {', '.join(f.replace('<login>', d['name']).replace('<repo>', d['name']) for f in d['files'])}")
        print("in sync" if not diffs else f"{len(diffs)} difference(s); follow the vault-roster skill")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
