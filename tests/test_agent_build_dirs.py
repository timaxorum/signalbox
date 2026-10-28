"""Build roles receive writable cache and temporary directories (#451)."""

from types import SimpleNamespace

import pytest

from signalbox import agent


@pytest.fixture
def calls(monkeypatch, tmp_path):
    recorded = []
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    monkeypatch.setenv("SIGNALBOX_RUNNER", "claude")
    monkeypatch.setattr(agent, "_workdir", lambda payload: str(tmp_path))
    monkeypatch.setattr(
        agent.subprocess,
        "run",
        lambda command, **kwargs: recorded.append((command, kwargs))
        or SimpleNamespace(returncode=0, stdout='{"verdict":"done"}', stderr=""),
    )
    monkeypatch.setattr(agent, "post_provenance", lambda *args, **kwargs: None)
    return recorded


@pytest.mark.parametrize("role", ["rebase", "remediate"])
def test_claude_build_roles_receive_build_dirs(calls, tmp_path, monkeypatch, role):
    monkeypatch.setenv("CARGO_TARGET_DIR", "/old-target")
    monkeypatch.setenv("TMPDIR", "/old-tmp")
    monkeypatch.setenv("BUILD_ENV_SENTINEL", "inherited")

    _, code = agent.run(role, {"run_id": "r1"}, model="opus")

    assert code == 0
    command, kwargs = calls[0]
    target = tmp_path / "target"
    tmp = tmp_path / "tmp" / "r1"
    assert [command[i + 1] for i, arg in enumerate(command) if arg == "--add-dir"] == [
        str(target),
        str(tmp),
    ]
    assert kwargs["env"]["CARGO_TARGET_DIR"] == str(target)
    assert kwargs["env"]["TMPDIR"] == str(tmp)
    assert kwargs["env"]["BUILD_ENV_SENTINEL"] == "inherited"
    assert target.is_dir()
    assert tmp.is_dir()


def test_review_receives_no_build_dirs(calls, tmp_path):
    _, code = agent.run("review", {"run_id": "r1"}, model="opus")

    assert code == 0
    command, kwargs = calls[0]
    assert "--add-dir" not in command
    # #473: every runner now gets an env (the pinned-pnpm shim on PATH);
    # what non-build roles must not get is the build directories.
    env = kwargs.get("env", {})
    assert env.get("CARGO_TARGET_DIR") != str(tmp_path / "target")
    assert env.get("TMPDIR") != str(tmp_path / "tmp")
    assert not (tmp_path / "target").exists()
    assert not (tmp_path / "tmp").exists()


@pytest.mark.parametrize("role", ["rebase", "remediate"])
def test_codex_build_roles_keep_stdin_last_and_no_claude_flags(
    calls, tmp_path, monkeypatch, role
):
    monkeypatch.setenv("SIGNALBOX_RUNNER", "codex")

    _, code = agent.run(role, {"run_id": "r1"}, model="gpt-test")

    assert code == 0
    command, kwargs = calls[0]
    assert command[0] == "codex"
    assert command[-1] == "-"
    assert "--add-dir" not in command
    # #473: every runner now gets an env (the pinned-pnpm shim on PATH);
    # what non-build roles must not get is the build directories.
    env = kwargs.get("env", {})
    assert env.get("CARGO_TARGET_DIR") != str(tmp_path / "target")
    assert env.get("TMPDIR") != str(tmp_path / "tmp")
    assert not (tmp_path / "target").exists()
    assert not (tmp_path / "tmp").exists()


def test_build_env_merges_with_vault_env(calls, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("SIGNALBOX_VAULT", str(vault))
    monkeypatch.setattr(agent, "VAULT_ROLES", agent.VAULT_ROLES | {"rebase"})
    monkeypatch.setattr(agent, "NOTES_ROLES", agent.NOTES_ROLES | {"rebase"})

    _, code = agent.run("rebase", {"run_id": "r1"}, model="opus")

    assert code == 0
    command, kwargs = calls[0]
    assert str(vault) in command
    assert kwargs["env"]["SIGNALBOX_VAULT"] == str(vault)
    assert kwargs["env"]["CARGO_TARGET_DIR"] == str(tmp_path / "target")
    assert kwargs["env"]["TMPDIR"] == str(tmp_path / "tmp" / "r1")
