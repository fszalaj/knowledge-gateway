#!/usr/bin/env bash
# Clone or fast-forward the source repositories listed in repos.txt into repos/<name>/ (WSL, Linux, macOS).
#
# Usage: scripts/sync-repos.sh [--only <name>] [--dry-run]
#
#   missing clone  -> git clone -c core.autocrlf=false <base><name> repos/<name>
#   existing clone -> skipped with a warning when `git status --porcelain` is not empty; otherwise
#                     git fetch --prune --quiet origin, and git merge --ff-only origin/<default> when the clone is
#                     on its default branch. Extra refs from repos.txt are only fetched (read them as origin/<ref>).
#
# The script never resets, stashes, cleans, switches branches or forces anything. Clone URLs are <base><name>: the
# base is REPOS_GIT_BASE when set, else the "# git-base: <url>" line of repos.txt (for example
# "# git-base: https://github.com/<owner>/"). Exit status 1 when any repository failed, 2 for usage errors.
set -euo pipefail

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
}

only=""
dry_run=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --only)
      [ "$#" -ge 2 ] || { echo "sync-repos: --only needs a repository name" >&2; exit 2; }
      only="$2"
      shift 2
      ;;
    --only=*)
      only="${1#--only=}"
      shift
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "sync-repos: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
list="$root/repos.txt"
[ -f "$list" ] || { echo "sync-repos: $list not found (copy repos.example.txt to repos.txt)" >&2; exit 2; }

base="${REPOS_GIT_BASE:-}"
if [ -z "$base" ]; then
  base="$(sed -n 's/^#[[:space:]]*git-base:[[:space:]]*\([^[:space:]]*\).*$/\1/p' "$list" | head -n 1 | tr -d '\r')"
fi
if [ -z "$base" ]; then
  echo "sync-repos: no clone base; add '# git-base: https://github.com/<owner>/' to repos.txt or set REPOS_GIT_BASE" >&2
  exit 2
fi
case "$base" in
  */) ;;
  *) base="$base/" ;;
esac
mkdir -p "$root/repos"

rows=()
failed=0
matched=0

add_row() { # name branch sha state
  rows+=("$1"$'\t'"$2"$'\t'"$3"$'\t'"$4")
}

warn() {
  echo "sync-repos: $*" >&2
}

head_info() { # dir -> "branch<TAB>sha"
  local dir="$1" branch sha
  branch="$(git -C "$dir" symbolic-ref --quiet --short HEAD 2>/dev/null || echo "(detached)")"
  sha="$(git -C "$dir" rev-parse --short HEAD 2>/dev/null || echo "-")"
  printf '%s\t%s' "$branch" "$sha"
}

sync_one() { # name default extra_refs...
  local name="$1" default="$2"
  shift 2
  local dir="$root/repos/$name" url="$base$name" state info branch sha ref

  if [ ! -e "$dir" ]; then
    if [ "$dry_run" -eq 1 ]; then
      add_row "$name" "-" "-" "would clone $url"
      return 0
    fi
    if ! git clone -c core.autocrlf=false "$url" "repos/$name"; then
      warn "$name: clone failed"
      add_row "$name" "-" "-" "FAILED (clone)"
      return 1
    fi
    state="cloned"
  else
    if [ ! -d "$dir/.git" ] || ! git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      warn "$name: repos/$name exists but is not a git clone; left untouched"
      add_row "$name" "-" "-" "FAILED (not a git clone)"
      return 1
    fi
    if [ -n "$(git -C "$dir" status --porcelain)" ]; then
      warn "$name: working tree has local changes; skipped (nothing fetched or merged)"
      info="$(head_info "$dir")"
      add_row "$name" "${info%%$'\t'*}" "${info##*$'\t'}" "skipped (local changes)"
      return 0
    fi
    branch="$(git -C "$dir" symbolic-ref --quiet --short HEAD 2>/dev/null || echo "(detached)")"
    if [ "$dry_run" -eq 1 ]; then
      info="$(head_info "$dir")"
      if [ "$branch" = "$default" ]; then
        state="would fetch and fast-forward $default"
      else
        state="would fetch only (on $branch, not $default)"
      fi
      add_row "$name" "${info%%$'\t'*}" "${info##*$'\t'}" "$state"
      return 0
    fi
    if ! git -C "$dir" fetch --prune --quiet origin; then
      warn "$name: fetch failed"
      info="$(head_info "$dir")"
      add_row "$name" "${info%%$'\t'*}" "${info##*$'\t'}" "FAILED (fetch)"
      return 1
    fi
    if [ "$branch" = "$default" ]; then
      if ! git -C "$dir" merge --ff-only --quiet "origin/$default"; then
        warn "$name: $default cannot be fast-forwarded to origin/$default (diverged); left as is"
        info="$(head_info "$dir")"
        add_row "$name" "${info%%$'\t'*}" "${info##*$'\t'}" "FAILED (not fast-forward)"
        return 1
      fi
      state="up to date"
    else
      warn "$name: on $branch, not $default; fetched only"
      state="fetched (on $branch)"
    fi
  fi

  for ref in "$@"; do
    if ! git -C "$dir" rev-parse --verify --quiet "refs/remotes/origin/$ref" >/dev/null; then
      if ! git -C "$dir" fetch --quiet origin "+refs/heads/$ref:refs/remotes/origin/$ref"; then
        warn "$name: extra ref $ref could not be fetched"
        info="$(head_info "$dir")"
        add_row "$name" "${info%%$'\t'*}" "${info##*$'\t'}" "FAILED (extra ref $ref)"
        return 1
      fi
    fi
    state="$state; origin/$ref $(git -C "$dir" rev-parse --short "refs/remotes/origin/$ref")"
  done

  info="$(head_info "$dir")"
  add_row "$name" "${info%%$'\t'*}" "${info##*$'\t'}" "$state"
  return 0
}

cd "$root"
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%%#*}"
  line="${line%$'\r'}"
  read -r -a fields <<<"$line" || true
  [ "${#fields[@]}" -eq 0 ] && continue
  name="${fields[0]}"
  if [ "${#fields[@]}" -lt 2 ]; then
    warn "repos.txt: '$line' has no default branch"
    failed=1
    continue
  fi
  case "$name" in
    . | .. | *[!A-Za-z0-9._-]*)
      warn "repos.txt: invalid repository name '$name'"
      failed=1
      continue
      ;;
  esac
  if [ -n "$only" ] && [ "$name" != "$only" ]; then
    continue
  fi
  matched=1
  if ! sync_one "${fields[@]}"; then
    failed=1
  fi
done <"$list"

if [ -n "$only" ] && [ "$matched" -eq 0 ]; then
  echo "sync-repos: '$only' is not listed in repos.txt" >&2
  exit 2
fi

printf '\n%-24s %-18s %-10s %s\n' "name" "branch" "sha" "state"
printf '%-24s %-18s %-10s %s\n' "----" "------" "---" "-----"
for row in "${rows[@]+"${rows[@]}"}"; do
  IFS=$'\t' read -r r_name r_branch r_sha r_state <<<"$row"
  printf '%-24s %-18s %-10s %s\n' "$r_name" "$r_branch" "$r_sha" "$r_state"
done

exit "$failed"
