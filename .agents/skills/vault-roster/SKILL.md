---
name: vault-roster
description: Use in a team vault when a member joins or leaves its GitHub team, when a repository is added to the team, archived, removed or renamed, or when scripts/roster_check.py reports a difference; keeps team.txt, repos.txt and every page that lists them in step.
---

# Keep a team vault's roster in step with its GitHub team

A team vault built from knowledge-gateway lists its members in `team.txt` (`<github-login> <maintainer|member>`)
and the repositories it describes in `repos.txt` (`<name> <default_branch> [<extra_ref>...]`); both start from the
`*.example.txt` templates. Other files usually repeat them: the vault's repository catalog and team roster pages, the
member pages `wiki/hot.<login>.md` and `wiki/team/<login>.md`, and, for Claude Code, the `claudeMdExcludes` list in
`.claude/settings.json` that keeps it from loading a clone's own instruction files. This skill changes only the vault.
Team membership and repository permissions on GitHub are changed in GitHub by an organization owner or a team
maintainer, never by an agent.

## When to use

- `python3 scripts/roster_check.py` (Windows: `py -3 scripts\roster_check.py`) reports a difference between the
  GitHub team named in `team.txt` (`# github-team: <org>/<team-slug>`) and `team.txt` / `repos.txt`.
- A vault lint or a scheduled roster check reports that the roster files disagree.
- Someone asks to add or remove a member or a repository.

## Before you start

1. Follow the vault's own `AGENTS.md`. Roster changes touch other members' pages, so they are made or reviewed by a
   vault maintainer; open a pull request, never commit to `main`.
2. Run `roster_check.py` and note every difference with the files it names.
3. Use `search` (or `git grep -n <name>`) to find every mention of the member or repository before you change it.

## A member joins

1. Add `<login> member` to `team.txt` with the exact case GitHub uses (`maintainer` only if the team decided so).
2. Create `wiki/team/<login>.md` (profile: GitHub team role, vault role, contributions) and `wiki/hot.<login>.md`
   (owner `<login>`; sections Last Updated, Current state, Next concrete step) from the vault's templates, with
   `write_note`.
3. Add the member to the vault's team roster page and, if the vault keeps one, its ownership or contribution table
   (`patch_note`).
4. Point the new member to the vault's onboarding prompt.

## A member leaves

1. Ask the user what of the member's hot page is still relevant and move it to shared pages; then, with the user's
   confirmation, delete the hot page (git history keeps it).
2. Keep the profile as history (`status: stale`, "Left the team on YYYY-MM-DD").
3. Remove the login from `team.txt` and from the roster page.
4. Reassign what the member owned (default owners, contacts named in the vault's skills) with the team.

## A repository is added to the team

1. Add `<name> <default_branch>` to `repos.txt` (the clone base is its `# git-base:` line).
2. Add the name to every `claudeMdExcludes` glob in `.claude/settings.json`, if the vault has that file.
3. Add the repository to the vault's repository catalog (and README table, if the vault has one).
4. Clone it: `scripts/sync-repos.sh --only <name>` (Windows: `scripts\sync-repos.ps1 -Only <name>`).
5. Describe it: pages under `wiki/sources/<name>/` with the vault's scan procedure, or `wiki-ingest`,
   `wiki-curate` and `obsidian-markdown`; build its graph with `code-graph-build` into `wiki/.graph/<name>.json`.

## A repository is archived, removed, renamed or transferred

- **Archived:** keep it in `repos.txt`; note the archive date on its pages and in the catalog, and stop re-scanning.
- **Removed or transferred out of the team:** ask the user before deleting anything. Remove it from `repos.txt`,
  `claudeMdExcludes` and the catalog; delete `wiki/sources/<name>/` and its graph files (git history keeps them) and
  fix the links that break. Each member removes the ignored clone `repos/<name>/` by hand.
- **Renamed:** rename it everywhere above, move its pages with `git mv`, update their frontmatter, and re-scan.
- **Default branch changed:** update `repos.txt` and the catalog, then re-scan.

## Finish

1. `roster_check.py` prints `in sync`, the vault's lint passes, and `git_status` shows only the roster changes.
2. Commit with your own identity and open a pull request for review.
