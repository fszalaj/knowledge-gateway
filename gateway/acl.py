from __future__ import annotations

from dataclasses import dataclass


class AccessDenied(Exception):
    """Raised when a token may not touch a vault. Message is deliberately opaque
    so a caller cannot tell a forbidden vault apart from a non-existent one."""


@dataclass(frozen=True)
class TokenInfo:
    sub: str
    vaults: frozenset[str]
    write: bool
    email: str = ""


def build_registry(tokens_cfg: dict) -> dict[str, TokenInfo]:
    reg: dict[str, TokenInfo] = {}
    for token, meta in (tokens_cfg or {}).items():
        if not isinstance(token, str) or not token:
            raise ValueError("token key must be a non-empty string")
        sub = meta.get("sub", "unknown")
        vaults = meta.get("vaults", [])
        write = meta.get("write", False)
        # Validate types explicitly: `vaults: myvault` (a str) would otherwise
        # become a frozenset of characters, and `write: "false"` (a str) would be
        # truthy via bool() and silently grant write access.
        if not isinstance(vaults, list) or not all(isinstance(x, str) for x in vaults):
            raise ValueError(f"token '{sub}': vaults must be a list of strings")
        if not isinstance(write, bool):
            raise ValueError(f"token '{sub}': write must be a boolean (true/false)")
        reg[token] = TokenInfo(
            sub=str(sub),
            vaults=frozenset(vaults),
            write=write,
            email=str(meta.get("email", "")),
        )
    return reg


def scopes_for(info: TokenInfo) -> list[str]:
    scopes = [f"vault:{v}" for v in sorted(info.vaults)]
    if info.write:
        scopes.append("write")
    return scopes


def allowed_vaults(scopes) -> set[str]:
    return {s.split(":", 1)[1] for s in scopes if s.startswith("vault:")}


def can_write(scopes) -> bool:
    return "write" in set(scopes)


def authorize(scopes, vault: str, *, write: bool) -> None:
    if vault not in allowed_vaults(scopes):
        raise AccessDenied(f"vault_forbidden: {vault}")
    if write and not can_write(scopes):
        raise AccessDenied(f"write_forbidden: {vault}")
