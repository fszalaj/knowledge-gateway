# Optional deployment: expose the shared-server gateway over Tailscale

This recipe uses Tailscale Serve as one option for protected remote access. Tailscale is
not a gateway requirement: an HTTPS reverse proxy, encrypted SSH tunnel, or another encrypted
VPN can provide the transport instead. See the [shared server requirements](../README.md#shared-server-mode).

In this recipe, the gateway listens on `127.0.0.1:8765` only. Tailscale Serve is the private
HTTPS front door - reachable **inside the tailnet only**. This recipe does not use
`tailscale funnel` to expose an internet endpoint.

## 1. Serve it (auto-TLS + MagicDNS)

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8765
tailscale serve status   # -> https://YOUR-GATEWAY-HOST.<tailnet>.ts.net/ -> 127.0.0.1:8765
```

The MCP endpoint is then `https://YOUR-GATEWAY-HOST.<tailnet>.ts.net/mcp/`.

## 2. Restrict to the team (tailnet policy / ACL)

Tag the node and allow only the team group to reach port 443:

```jsonc
{
  "tagOwners": { "tag:mcp": ["autogroup:admin"] },
  "groups":    { "group:team": ["alice@example.com", "bob@example.com"] },
  "acls": [
    { "action": "accept", "src": ["group:team"], "dst": ["tag:mcp:443"] }
  ]
}
```

Then on the gateway host: `tailscale up --advertise-tags=tag:mcp`.

## 3. Connect a client (identical UX to the GitHub MCP server)

```bash
export GW_TOKEN=...   # the user's own token from tokens.yaml, shared securely
claude mcp add --transport http --scope project teamwiki \
  https://YOUR-GATEWAY-HOST.<tailnet>.ts.net/mcp/ \
  --header "Authorization: Bearer $GW_TOKEN"
```

In this deployment, three independent layers guard the endpoint: tailnet ACL (network) +
HTTPS (transport) + per-user bearer/ACL (application).
