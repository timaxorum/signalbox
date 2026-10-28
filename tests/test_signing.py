"""Real Git coverage for worktree signing and post-rebase repair."""

import subprocess
from pathlib import Path

import pytest

from signalbox import acts


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def signing_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path / "state"))
    monkeypatch.delenv("SIGNALBOX_VAULT", raising=False)
    key = tmp_path / "key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True, capture_output=True,
    )
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-q", "-b", "main")
    source = tmp_path / "repo"
    git(tmp_path, "clone", str(origin), str(source))
    for name, value in (
        ("gpg.format", "ssh"), ("user.signingkey", str(key) + ".pub"),
        ("user.name", "Signing Test"), ("user.email", "sign@example.test"),
        ("commit.gpgsign", "true"),
    ):
        git(source, "config", name, value)
    git(source, "commit", "--allow-empty", "-qm", "base")
    git(source, "push", "origin", "main")
    base = git(source, "rev-parse", "HEAD")
    monkeypatch.setattr(acts, "install_skills", lambda root: [])
    payload = acts.prepare_workspace({
        "run_id": "sign", "repo_path": str(source),
        "base_branch": "main", "base_sha": base, "rebased_onto_sha": base,
    })
    assert payload["ok"] is True, payload
    return source, Path(payload["worktree"]), payload


def commit_signing_changes(root, *, signed):
    for index in range(2):
        (root / f"change-{index}").write_text(str(index))
        git(root, "add", ".")
        git(root, "commit", "-qm", f"change {index}",
            "-S" if signed else "--no-gpg-sign")
    return git(root, "rev-list", "origin/main..HEAD").splitlines()


def test_prepare_workspace_enables_worktree_signing(signing_repo):
    source, root, payload = signing_repo
    assert git(root, "config", "--worktree", "--get", "commit.gpgsign") == "true"
    # Re-entry restores signing even if an earlier agent changed the setting.
    git(root, "config", "--worktree", "commit.gpgsign", "false")
    assert acts.prepare_workspace(payload)["ok"] is True
    assert git(root, "config", "--worktree", "--get", "commit.gpgsign") == "true"
    assert git(source, "config", "--local", "--get", "user.signingkey")
    config = git(root, "rev-parse", "--git-path", "config.worktree")
    assert "user.signingkey" not in (root / config).read_text()


def test_verify_signatures_keeps_signed_rebased_commits(signing_repo):
    source, root, payload = signing_repo
    original = commit_signing_changes(root, signed=True)
    git(source, "commit", "--allow-empty", "-qm", "upstream")
    git(source, "push", "origin", "main")
    payload = {**payload, "rebased_onto_sha": git(source, "rev-parse", "HEAD")}
    git(root, "rebase", "origin/main")
    rebased = git(root, "rev-list", "origin/main..HEAD").splitlines()
    assert rebased != original
    result = acts.verify_signatures(payload)
    assert result["ok"] is True
    assert result["resigned_shas"] == []
    assert result["rebased_onto_sha"] == payload["rebased_onto_sha"]
    assert git(root, "rev-list", "origin/main..HEAD").splitlines() == rebased


def test_verify_signatures_resigns_unsigned_commits(signing_repo):
    _, root, payload = signing_repo
    original = commit_signing_changes(root, signed=False)
    result = acts.verify_signatures(payload)
    assert result["ok"] is True, result
    assert result["resigned_shas"] == original
    assert result["rebased_onto_sha"] == payload["rebased_onto_sha"]
    rewritten = git(root, "rev-list", "origin/main..HEAD").splitlines()
    assert len(rewritten) == len(original)
    assert not set(rewritten).intersection(original)
    for sha in rewritten:
        assert "\ngpgsig " in git(root, "cat-file", "-p", sha)


def test_verify_signatures_aborts_failed_resigning(signing_repo):
    _, root, payload = signing_repo
    original = commit_signing_changes(root, signed=False)
    git(root, "config", "--worktree", "user.signingkey", str(root / "missing-key"))
    result = acts.verify_signatures(payload)
    assert result["ok"] is False
    assert result["reason"] == "unsigned commit after rebase"
    assert result["error"]
    assert git(root, "rev-list", "origin/main..HEAD").splitlines() == original
    for name in ("rebase-merge", "rebase-apply"):
        assert not (root / git(root, "rev-parse", "--git-path", name)).exists()


@pytest.mark.parametrize("reentry", [False, True])
@pytest.mark.parametrize("failed_config", ["extensions.worktreeConfig", "--worktree"])
def test_prepare_workspace_reports_signing_config_failure(
    signing_repo, monkeypatch, reentry, failed_config,
):
    source, root, payload = signing_repo
    if not reentry:
        git(source, "worktree", "remove", str(root))
        git(source, "branch", "-D", payload["branch"])
    real_run = acts._run

    def run(cmd, cwd=None, timeout=120):
        if cmd[:2] == ["git", "config"] and cmd[2] == failed_config:
            return 1, "", "config refused"
        return real_run(cmd, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(acts, "_run", run)
    result = acts.prepare_workspace(payload)
    assert result["ok"] is False
    assert result["error"] == "config refused"
    assert result["run_id"] == payload["run_id"]
