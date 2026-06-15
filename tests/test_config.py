import textwrap

import pytest

from gateway import config


def _write(tmp_path, body: str):
    p = tmp_path / "vaults.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_loads_valid_vaults(tmp_path):
    cfg = _write(
        tmp_path,
        f"""
        vaults:
          a:
            path: {tmp_path}/a
            repo_root: {tmp_path}/a
            subdir: "."
          b:
            path: {tmp_path}/b/wiki
            repo_root: {tmp_path}/b
            subdir: wiki
        """,
    )
    vaults = config.load_vaults(cfg)
    assert set(vaults) == {"a", "b"}
    assert vaults["b"].subdir == "wiki"


def test_rejects_dot_subdir_with_parent_repo(tmp_path):
    cfg = _write(
        tmp_path,
        f"""
        vaults:
          bad:
            path: {tmp_path}/repo/wiki
            repo_root: {tmp_path}/repo
            subdir: "."
        """,
    )
    with pytest.raises(ValueError):
        config.load_vaults(cfg)


def test_rejects_subdir_not_matching_path(tmp_path):
    # subdir points at a sibling tree (backend) instead of where path lives.
    cfg = _write(
        tmp_path,
        f"""
        vaults:
          bad:
            path: {tmp_path}/repo/wiki
            repo_root: {tmp_path}/repo
            subdir: backend
        """,
    )
    with pytest.raises(ValueError):
        config.load_vaults(cfg)


def test_rejects_overlapping_vault_paths(tmp_path):
    cfg = _write(
        tmp_path,
        f"""
        vaults:
          outer:
            path: {tmp_path}/x
            repo_root: {tmp_path}/x
            subdir: "."
          inner:
            path: {tmp_path}/x/sub
            repo_root: {tmp_path}/x/sub
            subdir: "."
        """,
    )
    with pytest.raises(ValueError):
        config.load_vaults(cfg)
