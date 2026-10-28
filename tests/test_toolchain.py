"""Pinned pnpm preparation and suites, without network access."""

import json
import os
import subprocess

import pytest

from signalbox import acts
from signalbox.toolchain import install_env, pnpm_argv, pnpm_packages, pnpm_version


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


def package(root, manager="pnpm@10.31.0"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps({
        "packageManager": manager, "scripts": {"test": "vitest"},
    }))


def test_pnpm_version_package_manager(tmp_path):
    package(tmp_path, "pnpm@10.31.0+sha512.abc")
    assert pnpm_version(tmp_path) == "10.31.0"
    assert pnpm_argv(tmp_path) == ["npx", "-y", "pnpm@10.31.0"]
    workflow = tmp_path / ".github/workflows/a.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("steps:\n  - uses: pnpm/action-setup@v4\n    with:\n      version: 9.2.1\n")
    assert pnpm_version(tmp_path) == "10.31.0"


def test_pnpm_version_workflow_fallback(tmp_path):
    package(tmp_path, "pnpm@^10.0.0")
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "z.yml").write_text(
        "steps:\n  - uses: pnpm/action-setup@v4\n    with:\n      version: 9.0.0\n")
    (workflows / "a.yml").write_text(
        "steps:\n  - uses: other/action@v1\n    with:\n      version: 1.2.3\n"
        "  - uses: pnpm/action-setup@v4\n    with:\n      version: '^10.0.0'\n"
        "  - name: pnpm\n    uses: pnpm/action-setup@v4\n    with:\n      run_install: false\n"
        "      version: '10.31.0' # pinned\n")
    assert pnpm_version(tmp_path) == "10.31.0"


def test_pnpm_version_ignores_ranges(tmp_path):
    for manager in ("pnpm@^10.0.0", "npm@10.31.0", "pnpm@latest"):
        package(tmp_path, manager)
        assert pnpm_version(tmp_path) is None
        assert pnpm_argv(tmp_path) == ["pnpm"]
    (tmp_path / "package.json").write_text("{broken")
    assert pnpm_version(tmp_path) is None


def test_pnpm_packages_tracked_lockfiles(tmp_path):
    git(tmp_path, "init")
    for name in ("z", "a", "untracked"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "pnpm-lock.yaml").write_text("")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    git(tmp_path, "add", "a/pnpm-lock.yaml", "z/pnpm-lock.yaml", "pnpm-lock.yaml")
    assert pnpm_packages(tmp_path) == [tmp_path, tmp_path / "a", tmp_path / "z"]
    (tmp_path / "pnpm-workspace.yaml").write_text("packages: ['*']")
    assert pnpm_packages(tmp_path) == [tmp_path]


def test_pnpm_packages_without_lockfile(tmp_path):
    assert pnpm_packages(tmp_path / "not-created") == []
    git(tmp_path, "init")
    assert pnpm_packages(tmp_path) == []
    env = install_env(tmp_path)
    assert env["CI"] == "true"
    assert env["npm_config_yes"] == "true"
    assert env["npm_config_store_dir"] == str(tmp_path / "pnpm-store")
    assert env["PATH"] == os.environ["PATH"]


def setup_prepare(tmp_path, monkeypatch, *, existing, failure=False):
    source = tmp_path / "repo"
    source.mkdir()
    git(source, "init", "-b", "main")
    package(source)
    for name in ("a", "z"):
        directory = source / name
        package(directory)
        (directory / "pnpm-lock.yaml").write_text("")
    git(source, "add", ".")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
    sha = git(source, "rev-parse", "HEAD")
    state = tmp_path / "state"
    monkeypatch.setenv("SIGNALBOX_STATE", str(state))
    monkeypatch.delenv("SIGNALBOX_VAULT", raising=False)
    monkeypatch.setattr(acts, "install_skills", lambda root: [])
    payload = {"run_id": "pnpm-test", "base_branch": "main",
               "base_sha": sha, "repo_path": str(source)}
    root = acts.worktree_for(payload)
    if existing:
        root.parent.mkdir(parents=True)
        git(source, "worktree", "add", "-b", acts.branch_for(payload), str(root), sha)
    real_run = subprocess.run
    calls = []

    def run(cmd, **kwargs):
        if cmd[0] in ("npx", "pnpm"):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 1 if failure else 0, "", "x" * 450 + "tail")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(acts.subprocess, "run", run)
    return acts.prepare_workspace(payload), root, state, calls


@pytest.mark.parametrize("existing", [False, True])
def test_prepare_workspace_installs_pinned_pnpm(tmp_path, monkeypatch, existing):
    result, root, state, calls = setup_prepare(tmp_path, monkeypatch, existing=existing)
    assert result["ok"] is True
    assert result["deps"] == [{"dir": str(root / n), "ok": True} for n in ("a", "z")]
    assert len(calls) == 2
    for (cmd, kwargs), name in zip(calls, ("a", "z")):
        assert cmd == ["npx", "-y", "pnpm@10.31.0", "install",
                       "--frozen-lockfile", "--prod=false"]
        assert kwargs["cwd"] == str(root / name)
        assert kwargs["timeout"] == 600
        assert kwargs["env"]["CI"] == "true"
        assert kwargs["env"]["npm_config_store_dir"] == str(state / "pnpm-store")


def test_prepare_workspace_install_failure_is_nonfatal(tmp_path, monkeypatch):
    result, root, _, calls = setup_prepare(tmp_path, monkeypatch, existing=False, failure=True)
    assert result["ok"] is True
    assert len(calls) == 2
    assert result["deps"] == [
        {"dir": str(root / n), "ok": False, "error": ("x" * 450 + "tail")[-400:]}
        for n in ("a", "z")
    ]


@pytest.mark.parametrize("diff", [False, True])
def test_run_suite_uses_pinned_pnpm(tmp_path, monkeypatch, diff):
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    root = tmp_path / "worktrees/suite"
    directory = root / "client" if diff else root
    package(directory)
    monkeypatch.setattr(acts.shutil, "which", lambda name: name if name == "npx" else None)
    assert acts.suite_command(directory) == ["npx", "-y", "pnpm@10.31.0", "test"]
    calls = []

    def run(cmd, **kwargs):
        if cmd[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(cmd, 0, "client/index.ts", "")
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "2 passed", "")

    monkeypatch.setattr(acts.subprocess, "run", run)
    result = acts.run_suite({"run_id": "suite", **({"base_sha": "base"} if diff else {})})
    assert result["ok"] is True
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == ["npx", "-y", "pnpm@10.31.0", "test"]
    assert kwargs["cwd"] == str(directory)
    assert kwargs["env"]["CI"] == "true"
    assert kwargs["env"]["npm_config_store_dir"] == str(tmp_path / "pnpm-store")
    monkeypatch.setattr(acts.shutil, "which", lambda name: name if name == "npm" else None)
    assert acts.suite_command(directory) == ["npm", "test"]
