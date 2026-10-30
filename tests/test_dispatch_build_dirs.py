"""Building dispatches receive writable caches and run-scoped temporary space."""

import json
import os
from types import SimpleNamespace

import pytest

from signalbox import dispatch, paths


@pytest.mark.parametrize("skill", ["signalbox-implement", "signalbox-fix"])
@pytest.mark.parametrize("runner", ["claude", "codex"])
@pytest.mark.parametrize("resumed", [False, True])
def test_dispatch_grants_build_directories(monkeypatch, tmp_path, skill, runner, resumed):
    # #473: dispatched implement/fix processes must use the repository pin.
    (tmp_path / "package.json").write_text(json.dumps({"packageManager": "pnpm@10.31.0"}))
    calls = []
    payload = {
        "run_id": "r1",
        "shard_id": "a",
        "round": 2 if resumed else 1,
        "declared": [],
    }
    target = tmp_path / "target"
    tmp = tmp_path / "tmp" / "r1"
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    monkeypatch.delenv("SIGNALBOX_RUNNER", raising=False)
    # Inherited settings must not override this run's paths.
    monkeypatch.setenv("CARGO_TARGET_DIR", "/old/target")
    monkeypatch.setenv("TMPDIR", "/old/tmp")
    monkeypatch.setattr(dispatch, "worktree_for", lambda payload: tmp_path)
    monkeypatch.setattr(dispatch, "require_skill", lambda *args: None)
    monkeypatch.setattr(
        dispatch,
        "read_session",
        lambda *args: (
            {"runner": runner, "session_id": "session-123"} if resumed else None
        ),
    )
    monkeypatch.setattr(dispatch, "record_session", lambda *args: None)
    monkeypatch.setattr(dispatch, "post_provenance", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatch.sys, "stdin", SimpleNamespace(read=lambda: json.dumps(payload))
    )

    def run(command, **kwargs):
        assert target.is_dir()
        assert tmp.is_dir()
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(dispatch.subprocess, "run", run)
    # Exercise the default codex mapping as well as the explicit Claude override.
    args = ["--skill", skill]
    if runner == "claude":
        args += ["--runner", runner]
    assert dispatch.main(args) == 0
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert kwargs["env"]["PATH"].split(os.pathsep)[0] == str(tmp_path / "shims/pnpm-10.31.0")
    assert kwargs["env"]["CARGO_TARGET_DIR"] == str(target)
    assert kwargs["env"]["TMPDIR"] == str(paths.run_tmp_link("r1"))
    assert command[0] == runner
    if runner == "claude":
        granted = [
            command[i + 1]
            for i, argument in enumerate(command)
            if argument == "--add-dir"
        ]
        assert granted == [str(target), str(tmp)]
        assert ("--resume" in command) == resumed
    else:
        settings = [
            command[i + 1]
            for i, argument in enumerate(command)
            if argument == "-c"
        ]
        roots = next(
            setting.split("=", 1)[1]
            for setting in settings
            if setting.startswith("sandbox_workspace_write.writable_roots=")
        )
        assert json.loads(roots) == [str(target), str(tmp)]
        assert 'shell_environment_policy.inherit="all"' in settings
        assert command[-1] == "-"
        if resumed:
            assert command[:3] == ["codex", "exec", "resume"]
            assert command[-2] == "session-123"
            assert "-C" not in command
            assert "--sandbox" not in command
            assert 'sandbox_mode="workspace-write"' in settings
        else:
            assert command[command.index("--sandbox") + 1] == "workspace-write"
