#!/bin/sh
# Daily: update this host's gateway to the latest @stable; reinstall + restart only if it moved.
export PATH="$HOME/.local/bin:$PATH"
SHA_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/obsidian-gateway-stable.sha"
mkdir -p "$(dirname "$SHA_FILE")"
remote=$(git ls-remote https://github.com/fszalaj/obsidian-gateway stable 2>/dev/null | awk '{print $1}')
[ -z "$remote" ] && exit 0
[ "$remote" = "$(cat "$SHA_FILE" 2>/dev/null)" ] && exit 0
uv tool install --reinstall --from "git+https://github.com/fszalaj/obsidian-gateway@stable" obsidian-gateway || exit 1
systemctl --user restart obsidian-gateway || exit 1
echo "$remote" > "$SHA_FILE"
