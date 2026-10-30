import pytest

from signalbox import paths


@pytest.fixture(autouse=True)
def short_tmp(monkeypatch, tmp_path):
    root = tmp_path / "short"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("SIGNALBOX_SHORT_TMP", str(root))
    monkeypatch.setattr(paths, "SHORT_TMP_BUDGET", 4096)
