#!/bin/sh
# Daily: update this host's gateway to the latest PyPI release; reinstall + restart only if
# the published version moved.
export PATH="$HOME/.local/bin:$PATH"

latest=$(curl -fsS https://pypi.org/pypi/knowledge-gateway/json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null)
[ -z "$latest" ] && exit 0

# The installed version is the state, so a half-finished update heals on the next run -
# unlike a cached marker file, which can claim success the install never reached.
installed=$(uv tool list 2>/dev/null | awk '/^knowledge-gateway /{print substr($2,2)}')
[ "$latest" = "$installed" ] && exit 0

# Carry the extras: a bare reinstall would silently downgrade the server to the core
# vault tools and take the graph and convert tools off the air.
uv tool install --reinstall "knowledge-gateway[graph,convert]==$latest" || exit 1
systemctl --user restart knowledge-gateway || exit 1
echo "knowledge-gateway ${installed:-none} -> $latest"
