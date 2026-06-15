from __future__ import annotations

import os
from pathlib import Path

import yaml

from .vaults import Vault

DEFAULT_VAULTS_FILE = "vaults.yaml"
DEFAULT_TOKENS_FILE = "tokens.yaml"


def _base_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve(env_var: str, default_name: str) -> Path:
    override = os.environ.get(env_var)
    if override:
        return Path(override).expanduser().resolve()
    return _base_dir() / default_name


def load_vaults(path: Path | None = None) -> dict[str, Vault]:
    cfg = path or _resolve("OBSIDIAN_GATEWAY_VAULTS", DEFAULT_VAULTS_FILE)
    if not cfg.exists():
        raise FileNotFoundError(
            f"vaults config not found: {cfg} — copy vaults.example.yaml to vaults.yaml"
        )
    data = yaml.safe_load(cfg.read_text()) or {}
    vaults = {
        name: Vault.from_spec(name, spec)
        for name, spec in (data.get("vaults") or {}).items()
    }
    if not vaults:
        raise ValueError(f"no vaults defined in {cfg}")

    items = list(vaults.values())
    for v in items:
        if v.subdir == "." and v.repo_root != v.path:
            raise ValueError(
                f"vault '{v.name}': subdir '.' with repo_root != path would commit the whole repo"
            )
        if v.subdir.startswith("/") or ".." in Path(v.subdir).parts:
            raise ValueError(f"vault '{v.name}': subdir must be a relative path without '..'")
        # subdir must actually locate path under repo_root, else a commit scoped
        # to the subdir could touch a sibling tree (e.g. backend/) the vault
        # token was never granted.
        if (v.repo_root / v.subdir).resolve() != v.path:
            raise ValueError(
                f"vault '{v.name}': repo_root/subdir does not resolve to path"
            )
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if a.path == b.path or a.path.is_relative_to(b.path) or b.path.is_relative_to(a.path):
                raise ValueError(
                    f"vault paths overlap ('{a.name}', '{b.name}') — grants would leak across them"
                )
    return vaults


def load_tokens(path: Path | None = None) -> dict:
    cfg = path or _resolve("OBSIDIAN_GATEWAY_TOKENS", DEFAULT_TOKENS_FILE)
    if not cfg.exists():
        raise FileNotFoundError(
            f"tokens config not found: {cfg} — copy tokens.example.yaml to tokens.yaml"
        )
    data = yaml.safe_load(cfg.read_text()) or {}
    return data.get("tokens") or {}
