from __future__ import annotations

from pathlib import Path

from .vaults import EXCLUDE_DIRS


class VaultDetectionError(ValueError):
    """No vault (or an ambiguous set) could be auto-detected in the cwd."""


def _dirs(d: Path) -> list[Path]:
    return [p for p in d.iterdir() if p.is_dir() and p.name not in EXCLUDE_DIRS and not p.name.startswith(".")]


def _child(d: Path, name: str) -> Path | None:
    # exact-name scan (not FS case-folding) so macOS and Linux agree
    for p in d.iterdir():
        if p.is_dir() and p.name == name:
            return p
    return None


def _is_vault(d: Path) -> bool:
    return (d / ".obsidian").is_dir() or any(p.suffix == ".md" for p in d.iterdir() if p.is_file())


def detect_vault(cwd: Path) -> Path:
    """Find the vault to serve when --vault is omitted. Precedence: cwd if it is itself a
    vault (.obsidian/), then ./wiki (a real vault), a single real ./*-obsidian-vault, a
    single child dir with .obsidian/. Same-tier multiplicity, or nothing, raises
    VaultDetectionError (pass --vault)."""
    cwd = cwd.resolve()
    if (cwd / ".obsidian").is_dir():  # cwd itself is a vault (wins over a wiki/ subfolder)
        return cwd
    wiki = _child(cwd, "wiki")
    if wiki and _is_vault(wiki):
        return wiki
    ov = [d for d in _dirs(cwd) if d.name.endswith("-obsidian-vault") and _is_vault(d)]
    if len(ov) > 1:
        raise VaultDetectionError(f"ambiguous: multiple *-obsidian-vault dirs: {sorted(d.name for d in ov)}")
    if ov:
        return ov[0]
    withobs = [d for d in _dirs(cwd) if (d / ".obsidian").is_dir()]
    if len(withobs) > 1:
        raise VaultDetectionError(f"ambiguous: multiple dirs with .obsidian/: {sorted(d.name for d in withobs)}")
    if withobs:
        return withobs[0]
    raise VaultDetectionError(f"no vault found in {cwd} - pass --vault <dir>")
