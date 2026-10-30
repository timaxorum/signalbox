import os
from pathlib import Path

import pytest

from signalbox import acts, paths


def test_run_tmp_link_is_short_and_stable(monkeypatch):
    monkeypatch.delenv("SIGNALBOX_SHORT_TMP", raising=False)
    monkeypatch.delenv("SIGNALBOX_STATE", raising=False)
    monkeypatch.setattr(paths, "SHORT_TMP_BUDGET", 40)
    link = paths.run_tmp_link("r1")
    assert link.parent == Path("/tmp") / f"sbx-{os.getuid()}"
    assert link == paths.run_tmp_link("r1")
    assert link != paths.run_tmp_link("r2")
    assert len(str(paths.run_tmp_link("r" * 64))) <= 40


def test_socket_path_under_short_tmp_fits_sun_path(monkeypatch):
    monkeypatch.delenv("SIGNALBOX_SHORT_TMP", raising=False)
    monkeypatch.delenv("SIGNALBOX_STATE", raising=False)
    monkeypatch.setattr(paths, "SHORT_TMP_BUDGET", 40)
    link = paths.run_tmp_link("r" * 64)
    assert len(str(link) + "/axorum-compiler-" + "0" * 32 + "/checker.sock") <= 107


def test_ensure_run_tmp_links_into_state(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path / "state"))
    link = paths.ensure_run_tmp("r1")
    assert link.is_symlink()
    assert os.readlink(link) == str(paths.run_tmp_dir("r1"))
    (link / "output").write_text("hello")
    assert (paths.run_tmp_dir("r1") / "output").read_text() == "hello"
    assert paths.ensure_run_tmp("r1") == link


def test_ensure_run_tmp_refuses_an_overlong_link(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path / "state"))
    # A relative root keeps the configured path exactly 40 characters long.
    monkeypatch.chdir(tmp_path)
    root = Path("x" * 40)
    monkeypatch.setenv("SIGNALBOX_SHORT_TMP", str(root))
    monkeypatch.setattr(paths, "SHORT_TMP_BUDGET", 40)
    with pytest.raises(RuntimeError, match="exceeds"):
        paths.ensure_run_tmp("r1")
    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_ensure_run_tmp_refuses_a_foreign_or_open_root(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path / "state"))
    open_root = tmp_path / "open"
    open_root.mkdir(mode=0o755)
    open_root.chmod(0o755)
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    symlink_root = tmp_path / "symlink"
    symlink_root.symlink_to(private_root, target_is_directory=True)
    for root in (open_root, symlink_root):
        monkeypatch.setenv("SIGNALBOX_SHORT_TMP", str(root))
        with pytest.raises(RuntimeError, match=str(root)):
            paths.ensure_run_tmp("r1")
        assert not os.path.lexists(paths.run_tmp_link("r1"))
        assert list(root.iterdir()) == []


def test_release_workspace_removes_the_link_not_its_neighbours(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    link = paths.ensure_run_tmp("r1")
    other_link = paths.ensure_run_tmp("r2")
    (link / "output").write_text("temporary")
    (other_link / "output").write_text("keep")

    result = acts.release_workspace({"run_id": "r1"})

    assert result["ok"] is True
    assert result["released"] is False
    assert not os.path.lexists(link)
    assert not paths.run_tmp_dir("r1").exists()
    assert other_link.is_symlink()
    assert os.readlink(other_link) == str(paths.run_tmp_dir("r2"))
    assert (other_link / "output").read_text() == "keep"
    assert acts.release_workspace({"run_id": "r1"})["ok"] is True


def test_release_workspace_keeps_a_link_into_another_state(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    paths.run_tmp_dir("r1").mkdir(parents=True)
    foreign_tmp = tmp_path / "another-state" / "tmp" / "r1"
    foreign_tmp.mkdir(parents=True)
    (foreign_tmp / "output").write_text("keep")
    link = paths.run_tmp_link("r1")
    link.symlink_to(foreign_tmp, target_is_directory=True)

    result = acts.release_workspace({"run_id": "r1"})

    assert result["ok"] is True
    assert result["released"] is False
    assert not paths.run_tmp_dir("r1").exists()
    assert link.is_symlink()
    assert os.readlink(link) == str(foreign_tmp)
    assert (foreign_tmp / "output").read_text() == "keep"
