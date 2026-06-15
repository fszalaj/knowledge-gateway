import os
import stat

import pytest

import gateway.writes as w
from gateway.writes import atomic_write

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions only")


def test_atomic_write_creates_then_overwrites(tmp_path):
    p = tmp_path / "n.md"
    atomic_write(p, "one")
    assert p.read_text() == "one"
    atomic_write(p, "two")
    assert p.read_text() == "two"
    assert [x.name for x in tmp_path.iterdir()] == ["n.md"]  # no leftover temp files


@posix_only
def test_atomic_write_new_note_is_0644_not_0600(tmp_path):
    p = tmp_path / "n.md"
    atomic_write(p, "x")
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


@posix_only
def test_atomic_write_preserves_existing_mode(tmp_path):
    p = tmp_path / "n.md"
    p.write_text("x")
    p.chmod(0o640)
    atomic_write(p, "y")
    assert stat.S_IMODE(p.stat().st_mode) == 0o640


def test_atomic_write_failure_leaves_original_intact(tmp_path, monkeypatch):
    p = tmp_path / "n.md"
    atomic_write(p, "orig")

    def boom(*a, **k):
        raise OSError("replace failed")

    monkeypatch.setattr(w.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(p, "new")
    assert p.read_text() == "orig"
    assert [x.name for x in tmp_path.iterdir()] == ["n.md"]  # temp cleaned up
