"""Acts report outcomes as data. None of them decides what happens next."""

from __future__ import annotations

import fcntl
import json
import subprocess
from pathlib import Path

import pytest

from signalbox.acts import (
    approve,
    close_issue,
    closing_reference,
    commit_fix,
    clear_pending,
    clear_session,
    issue_state_is_closed,
    map_ci_findings,
    mark_pending,
    notify,
    prepare_workspace,
    pr_state_is_merged,
    push_branch,
    push_branch_command,
    read_session,
    reap,
    record_session,
    run_declared_scope,
    source_repo,
    stage_files,
    suite_command,
)
from signalbox.dispatch import environment, pending_path
from signalbox.paths import VaultMissing


def test_approve_replays_projected_identity_to_the_control_url(tmp_path, monkeypatch):
    from signalbox import emit

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    payload = {
        "event": "approval.requested",
        "run_id": "71-1234",
        "repo": "acme/widget",
        "issue": 71,
        "title": "Approval",
        "base_sha": "abc123",
        "base_branch": "main",
        "stage_id": "s1-core",
        "stage_count": 2,
        "extra": "not identity",
    }
    mark_pending("approval", payload)
    posted = []
    monkeypatch.setattr(emit, "control_url", lambda: "http://control.test/emit")
    monkeypatch.setattr(
        emit,
        "post",
        lambda body, url=None: posted.append((body, url)) or 202,
    )

    result = approve("71-1234")

    assert result["ok"] is True
    assert posted == [
        (
            {
                "event": "approval.granted",
                "run_id": "71-1234",
                "repo": "acme/widget",
                "issue": 71,
                "title": "Approval",
                "base_sha": "abc123",
                "base_branch": "main",
                "stage_id": "s1-core",
                "stage_count": 2,
            },
            "http://control.test/emit",
        )
    ]


def test_present_null_repo_never_becomes_none_in_approval_state(
    tmp_path, monkeypatch
):
    from signalbox import emit
    from signalbox.emit import identity_from_env

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    payload = {"run_id": "sb-106", "repo": None, "issue": 106}
    projected = identity_from_env(environment(payload, {}))
    marker = mark_pending("approval", projected)

    assert "None" not in marker.read_text()
    assert "repo" not in json.loads(marker.read_text())

    posted = []
    monkeypatch.setattr(emit, "post", lambda body, url=None: posted.append(body) or 202)
    result = approve("sb-106")

    assert result["ok"] is True
    assert "repo" not in posted[0]
    assert "None" not in json.dumps(posted[0])


def test_approve_without_a_marker_is_routed_false_data(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))

    result = approve("71-missing")

    assert result["ok"] is False
    assert result["run_id"] == "71-missing"
    assert "no approval marker" in result["error"]


def test_notify_prints_live_approval_invocation_only_for_approval_requested(capsys):
    notify(
        {
            "decision": "needs_human",
            "run_id": "71-1234",
        }
    )
    assert capsys.readouterr().out.splitlines() == [
        "[signalbox] needs_human: run=71-1234 shard=None",
        "signalbox approve 71-1234",
    ]

    notify({"event": "run.halted", "reason": "blocked", "run_id": "71-1234"})
    assert "signalbox approve" not in capsys.readouterr().out


def test_operator_approval_does_not_widen_agent_events():
    from signalbox.emit import ALLOWED_EVENTS

    assert ALLOWED_EVENTS == frozenset(
        {"shard.file-written", "shard.check-ran", "shard.submitted"}
    )


def _prepare_workspace(tmp_path, monkeypatch, *, existing=False):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    def fake_run(cmd, cwd=None, timeout=120):
        if cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"] and existing:
            return (0, "signalbox/run-sb-70", "")
        return (0, "991a9d06e9d099d897556db56d30632743f536c9", "")

    monkeypatch.setattr(acts, "_run", fake_run)
    monkeypatch.setattr(acts, "install_skills", lambda root: [])
    if existing:
        (tmp_path / "worktrees" / "sb-70").mkdir(parents=True)
    return prepare_workspace(
        {"run_id": "sb-70", "base_branch": "main", "repo_path": str(tmp_path)}
    )


def test_prepare_workspace_preserves_direct_callers_without_a_vault(tmp_path, monkeypatch):
    """Startup owns requiring the vault; direct act callers remain compatible."""
    monkeypatch.delenv("SIGNALBOX_VAULT", raising=False)
    result = _prepare_workspace(tmp_path, monkeypatch)
    assert result["ok"] is True


def test_prepare_workspace_refuses_a_missing_vault_directory(tmp_path, monkeypatch):
    """#70: engine launch must recheck the vault that operator preflight saw."""
    missing = tmp_path / "missing-vault"
    monkeypatch.setenv("SIGNALBOX_VAULT", str(missing))
    with pytest.raises(
        VaultMissing,
        match=f"SIGNALBOX_VAULT is not a directory: {missing}",
    ):
        _prepare_workspace(tmp_path, monkeypatch)


def test_prepare_workspace_accepts_the_engine_environment_vault(tmp_path, monkeypatch):
    """#70: a resolved engine vault closes the preflight-environment gap."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("SIGNALBOX_VAULT", str(vault))
    result = _prepare_workspace(tmp_path, monkeypatch)
    assert result["ok"] is True


def test_prepare_workspace_resumes_an_existing_run_without_a_vault(tmp_path, monkeypatch):
    """An existing workspace predates this launch-time environment recheck."""
    monkeypatch.delenv("SIGNALBOX_VAULT", raising=False)
    result = _prepare_workspace(tmp_path, monkeypatch, existing=True)
    assert result["ok"] is True


def test_prepare_workspace_reentry_recovers_the_original_base_after_branch_moves(
    tmp_path, monkeypatch
):
    """A moving base branch cannot invalidate the workspace a run already uses."""
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    (tmp_path / "worktrees" / "sb-70").mkdir(parents=True)
    calls = []

    def fake_run(cmd, cwd=None, timeout=120):
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return (0, "signalbox/run-sb-70", "")
        if cmd == ["git", "merge-base", "HEAD", "main"]:
            return (0, "launch-pinned", "")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(acts, "_run", fake_run)
    monkeypatch.setattr(acts, "install_skills", lambda root: [])
    result = prepare_workspace({
        "run_id": "sb-70", "issue": 70, "base_branch": "main",
    })

    assert result["ok"] is True
    assert result["base_sha"] == "launch-pinned"
    assert calls == [
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        ["git", "merge-base", "HEAD", "main"],
    ]


def test_prepare_workspace_refuses_an_existing_tree_on_the_wrong_branch_with_identity(
    tmp_path, monkeypatch
):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    (tmp_path / "worktrees" / "sb-70").mkdir(parents=True)
    monkeypatch.setattr(
        acts, "_run", lambda *a, **k: (0, "some-other-branch", "")
    )

    result = prepare_workspace({
        "run_id": "sb-70", "issue": 70, "stage_id": "s1",
        "base_branch": "main", "base_sha": "launch-pinned",
    })

    assert result["ok"] is False
    assert result["run_id"] == "sb-70"
    assert result["issue"] == 70
    assert result["stage_id"] == "s1"


def test_prepare_workspace_refuses_an_unverified_created_tree_with_identity(
    tmp_path, monkeypatch
):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    monkeypatch.delenv("SIGNALBOX_VAULT", raising=False)
    responses = iter([(0, "", ""), (0, "wrong-head", "")])
    monkeypatch.setattr(acts, "_run", lambda *a, **k: next(responses))

    result = prepare_workspace({
        "run_id": "sb-70", "issue": 70, "stage_id": "s1",
        "base_branch": "main", "base_sha": "launch-pinned",
        "repo_path": str(tmp_path),
    })

    assert result["ok"] is False
    assert result["run_id"] == "sb-70"
    assert result["issue"] == 70
    assert result["stage_id"] == "s1"


def test_stage_files_is_the_union_of_declared_scopes():
    payload = {
        "results": [
            {"shard_id": "a", "declared": ["src/a.py", "src/shared.py"]},
            {"shard_id": "b", "declared": ["src/b.py"]},
        ]
    }
    assert stage_files(payload) == ["src/a.py", "src/shared.py", "src/b.py"]


def test_stage_files_deduplicates_and_preserves_order():
    payload = {"results": [{"declared": ["a", "b"]}, {"declared": ["b", "c"]}]}
    assert stage_files(payload) == ["a", "b", "c"]


def test_stage_files_of_an_empty_stage_is_empty():
    assert stage_files({}) == []


def test_push_branch_command_force_pushes_with_a_lease():
    assert push_branch_command("signalbox/run-sb-113") == [
        "git",
        "push",
        "--force-with-lease",
        "-u",
        "origin",
        "signalbox/run-sb-113",
    ]


def test_push_branch_runs_the_leased_command(tmp_path, monkeypatch):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    calls = []

    def fake_run(cmd, cwd=None, timeout=120):
        calls.append((cmd, cwd, timeout))
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return (0, "abc123", "")
        if cmd[:3] == ["git", "ls-remote", "--heads"]:
            return (0, "abc123\trefs/heads/signalbox/run-sb-113", "")
        return (0, "", "")

    monkeypatch.setattr(acts, "_run", fake_run)

    result = push_branch({"run_id": "sb-113"})

    assert result["ok"] is True
    assert calls[1] == (
        push_branch_command("signalbox/run-sb-113"),
        str(tmp_path / "worktrees" / "sb-113"),
        120,
    )


def test_push_branch_reports_push_failure_as_data(tmp_path, monkeypatch):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    responses = iter([
        (0, "abc123", ""),
        (1, "", "lease rejected"),
        (0, "", ""),
    ])
    monkeypatch.setattr(acts, "_run", lambda *a, **k: next(responses))

    result = push_branch({"run_id": "sb-113", "issue": 113})

    assert result["ok"] is False
    assert result["error"] == "lease rejected"
    assert result["run_id"] == "sb-113"
    assert result["issue"] == 113


def test_push_branch_trusts_verified_remote_state_after_command_error(tmp_path, monkeypatch):
    """A command error cannot negate a push that the remote confirms landed."""
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    responses = iter([
        (0, "abc123", ""),
        (1, "", "local tracking setup failed"),
        (0, "abc123\trefs/heads/signalbox/run-sb-113", ""),
    ])
    monkeypatch.setattr(acts, "_run", lambda *a, **k: next(responses))

    result = push_branch({"run_id": "sb-113"})

    assert result["ok"] is True
    assert result["sha"] == "abc123"


def test_push_branch_does_not_claim_success_without_remote_verification(
    tmp_path, monkeypatch
):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    responses = iter([
        (0, "abc123", ""),
        (0, "", ""),
        (0, "different\trefs/heads/signalbox/run-sb-113", ""),
    ])
    monkeypatch.setattr(acts, "_run", lambda *a, **k: next(responses))

    assert push_branch({"run_id": "sb-113"})["ok"] is False


def _merge_result(tmp_path, monkeypatch, responses):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    root = tmp_path / "worktrees" / "sb-65"
    root.mkdir(parents=True)
    calls = []

    def fake_run(cmd, cwd=None, timeout=120):
        calls.append((cmd, cwd, timeout))
        return responses.pop(0)

    monkeypatch.setattr(acts, "_run", fake_run)
    return acts.merge_pr({"run_id": "sb-65", "pr": 65}), calls


def test_merge_pr_reports_true_when_the_merge_lands_and_verifies(tmp_path, monkeypatch):
    """sb-65 landed, so the observed PR state must make ok true."""
    result, calls = _merge_result(
        tmp_path,
        monkeypatch,
        [
            (0, "", ""),
            (0, '{"state":"MERGED","mergedAt":"2026-07-29T00:00:00Z"}', ""),
            (0, "", ""),
        ],
    )
    assert result["ok"] is True
    assert calls[0][0] == ["gh", "pr", "merge", "65", "--squash"]
    assert calls[1][0] == ["gh", "pr", "view", "65", "--json", "state,mergedAt"]
    assert calls[2][0][-1].endswith("/signalbox/run-sb-65")
    assert all(call[1] == str(tmp_path / "worktrees" / "sb-65") for call in calls)


def test_merge_pr_reports_false_when_the_merge_does_not_land(tmp_path, monkeypatch):
    """Unlike sb-65, a rejected merge must remain false and retain gh's error."""
    result, calls = _merge_result(
        tmp_path,
        monkeypatch,
        [
            (1, "", "merge blocked by checks"),
            (0, '{"state":"OPEN","mergedAt":null}', ""),
        ],
    )
    assert result["ok"] is False
    assert result["error"] == "merge blocked by checks"
    assert len(calls) == 2


def test_merge_pr_trusts_merged_state_after_local_cleanup_error(tmp_path, monkeypatch):
    """sb-65 landed despite gh's worktree checkout failure, so ok must be true."""
    result, calls = _merge_result(
        tmp_path,
        monkeypatch,
        [
            (1, "", "fatal: branch is already checked out"),
            (0, '{"state":"MERGED","mergedAt":"2026-07-29T00:00:00Z"}', ""),
            (0, "", ""),
        ],
    )
    assert result["ok"] is True
    assert "error" not in result
    assert len(calls) == 3


def test_merge_pr_warns_when_remote_ref_survives_a_verified_merge(tmp_path, monkeypatch):
    """sb-65's surviving origin ref is cleanup trouble, not a failed merge."""
    result, _ = _merge_result(
        tmp_path,
        monkeypatch,
        [
            (0, "", ""),
            (0, '{"state":"MERGED","mergedAt":"2026-07-29T00:00:00Z"}', ""),
            (1, "", "ref deletion denied"),
        ],
    )
    assert result["ok"] is True
    assert result["warning"] == "ref deletion denied"


def test_pr_state_is_merged_judges_raw_json_without_a_network():
    assert pr_state_is_merged(
        '{"state":"MERGED","mergedAt":"2026-07-29T00:00:00Z"}'
    ) is True
    assert pr_state_is_merged('{"state":"OPEN","mergedAt":null}') is False
    assert pr_state_is_merged('{"state":"MERGED","mergedAt":null}') is False
    assert pr_state_is_merged("not json") is False
    assert pr_state_is_merged("[]") is False


def test_close_issue_closes_then_verifies_observed_state(monkeypatch):
    from signalbox import acts

    calls = []
    responses = iter([(0, "", ""), (0, '{"state":"CLOSED"}', "")])
    monkeypatch.setattr(
        acts, "_run",
        lambda cmd, cwd=None, timeout=120: (
            calls.append((cmd, cwd, timeout)) or next(responses)
        ),
    )

    result = close_issue(
        {"run_id": "sb-67", "repo": "acme/widget", "issue": 67}
    )

    assert result["ok"] is True
    assert calls[0][0] == ["gh", "issue", "close", "67", "--repo", "acme/widget"]
    assert calls[1][0] == [
        "gh", "issue", "view", "67", "--repo", "acme/widget", "--json", "state"
    ]


def test_close_issue_accepts_an_already_closed_issue(monkeypatch):
    from signalbox import acts

    responses = iter([
        (1, "", "issue is already closed"),
        (0, '{"state":"CLOSED"}', ""),
    ])
    monkeypatch.setattr(acts, "_run", lambda *args, **kwargs: next(responses))

    assert close_issue({"repo": "acme/widget", "issue": 67})["ok"] is True


def test_close_issue_without_a_repo_is_a_no_op(monkeypatch):
    from signalbox import acts

    calls = []
    monkeypatch.setattr(acts, "_run", lambda *args, **kwargs: calls.append(args))

    result = close_issue({"run_id": "sb-local", "issue": 67})

    assert result["ok"] is True
    assert calls == []


def test_close_issue_refusal_carries_run_identity(monkeypatch):
    from signalbox import acts

    responses = iter([(1, "", "permission denied"), (0, '{"state":"OPEN"}', "")])
    monkeypatch.setattr(acts, "_run", lambda *args, **kwargs: next(responses))

    result = close_issue(
        {"run_id": "sb-67", "repo": "acme/widget", "issue": 67}
    )

    assert result["ok"] is False
    assert result["run_id"] == "sb-67"
    assert result["error"] == "permission denied"


def test_issue_state_is_closed_judges_raw_json_without_a_network():
    assert issue_state_is_closed('{"state":"CLOSED"}') is True
    assert issue_state_is_closed('{"state":"OPEN"}') is False
    assert issue_state_is_closed("not json") is False
    assert issue_state_is_closed("[]") is False


def test_open_pr_keeps_the_issue_closing_trailer(tmp_path, monkeypatch):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    calls = []
    responses = iter([
        (0, "https://github.test/acme/widget/pull/67", ""),
        (
            0,
            '{"number":67,"url":"https://github.test/acme/widget/pull/67",'
            '"baseRefName":"main","headRefName":"signalbox/run-sb-67","state":"OPEN"}',
            "",
        ),
    ])
    monkeypatch.setattr(
        acts, "_run",
        lambda cmd, cwd=None, timeout=120: calls.append(cmd) or next(responses),
    )

    result = acts.open_pr(
        {"run_id": "sb-67", "issue": 67, "base_branch": "main", "title": "Close it"}
    )

    assert result["ok"] is True
    assert calls[0][calls[0].index("--body") + 1].startswith("Closes #67\n")


def test_open_pr_closes_the_issue_by_full_reference_when_the_run_names_its_repo(tmp_path, monkeypatch):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    calls = []
    responses = iter([
        (0, "https://github.test/acme/widget/pull/67", ""),
        (
            0,
            '{"number":67,"url":"https://github.test/acme/widget/pull/67",'
            '"baseRefName":"main","headRefName":"signalbox/run-sb-67","state":"OPEN"}',
            "",
        ),
    ])
    monkeypatch.setattr(
        acts, "_run",
        lambda cmd, cwd=None, timeout=120: calls.append(cmd) or next(responses),
    )

    result = acts.open_pr(
        {"run_id": "sb-67", "issue": 67, "base_branch": "main", "title": "Close it",
         "repo": "AxorumHQ/internal"}
    )

    assert result["ok"] is True
    assert calls[0][calls[0].index("--body") + 1].startswith("Closes AxorumHQ/internal#67\n")


def test_closing_reference_falls_back_to_a_bare_number_without_a_repo():
    assert closing_reference({"issue": 5}) == "#5"
    assert closing_reference({"issue": 5, "repo": None}) == "#5"
    assert closing_reference({"issue": 5, "repo": "a/b"}) == "a/b#5"


def test_ci_failure_becomes_review_findings():
    """The fix loop already exists; a red build just has to speak its vocabulary."""
    payload = {
        "pr": 12, "sha": "abc123", "run_id": "sb-101", "repo": "acme/widget",
        "check_runs_url": "https://api.example/check-suites/88/check-runs",
        "failed": [
            {
                "name": "build", "id": 91,
                "output": {"title": "Build failed", "summary": "compiler error"},
                "details_url": "https://ci.example/build",
                "html_url": "https://github.example/checks/91",
            },
            {"name": "clippy", "id": 92, "html_url": "https://github.example/checks/92"},
        ],
        "round": 2,
        "ci_round": 2,
        "stages": [
            {"shards": [{"files": ["src/z.py", "src/a.py"]}]},
            {"shards": [{"files": ["src/b.py", "src/a.py"]}]},
        ],
    }
    result = map_ci_findings(payload)
    assert result["ok"] is True
    assert result["verdict"] == "changes_requested"
    assert result["round"] == 1
    assert result["ci_origin"] == "post-merge"
    assert result["ci_round"] == 3
    assert result["shard_id"] == "ci-fix"
    assert result["intent"] == "Fix the failed post-merge CI checks"
    assert result["declared"] == ["src/a.py", "src/b.py", "src/z.py"]
    assert [f["check"] for f in result["findings"]] == ["build", "clippy"]
    assert all(f["source"] == "ci" for f in result["findings"])
    assert all(f["run_id"] == "sb-101" for f in result["findings"])
    assert result["findings"][0]["details_url"] == "https://ci.example/build"
    assert result["findings"][0]["html_url"] == "https://github.example/checks/91"

    def scalar_values(value):
        if isinstance(value, dict):
            return {item for child in value.values() for item in scalar_values(child)}
        if isinstance(value, list):
            return {item for child in value for item in scalar_values(child)}
        return {value}

    observed = scalar_values(payload)
    carried = scalar_values(result["findings"])
    assert carried - {"ci"} <= observed, "findings may point only to observed facts"
    assert "CI check failed after merge" not in carried


def test_run_declared_scope_uses_the_whole_plan_not_the_first_result():
    payload = {
        "declared": ["only/first-shard.py"],
        "results": [{"declared": ["also/not/the/run.py"]}],
        "stages": [
            {"shards": [
                {"files": ["src/b.py"]},
                {"files": ["src/a.py", "src/shared.py"]},
            ]},
            {"shards": [
                {"files": ["tests/test_b.py", "src/shared.py"]},
                {"files": ["tests/test_a.py"]},
            ]},
        ],
    }

    assert run_declared_scope(payload) == [
        "src/a.py", "src/b.py", "src/shared.py",
        "tests/test_a.py", "tests/test_b.py",
    ]


def test_map_ci_findings_refuses_an_empty_run_scope():
    result = map_ci_findings({
        "run_id": "sb-145",
        "failed": [{"name": "tests"}],
        "declared": ["first/shard.py"],
    })

    assert result["ok"] is False
    assert "declared run scope" in result["reason"]
    assert result["ci_origin"] == "post-merge"
    assert result["ci_round"] == 1
    assert "findings" not in result


def test_commit_fix_stages_declared_paths_commits_signed_and_verifies_head(
    tmp_path, monkeypatch
):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    calls = []
    responses = iter([
        (0, "", ""),
        (1, "", ""),
        (0, "", ""),
        (0, "fixed123", ""),
    ])

    def fake_run(cmd, cwd=None, timeout=120):
        calls.append(cmd)
        return next(responses)

    monkeypatch.setattr(acts, "_run", fake_run)
    result = commit_fix({
        "run_id": "sb-145", "issue": 145,
        "declared": ["src/a.py", "tests/test_a.py"],
    })

    assert result["ok"] is True
    assert result["sha"] == "fixed123"
    assert calls == [
        ["git", "add", "--", "src/a.py", "tests/test_a.py"],
        ["git", "diff", "--cached", "--quiet"],
        ["git", "commit", "-S", "-m", "fix(ci): 145"],
        ["git", "rev-parse", "HEAD"],
    ]


def test_commit_fix_refuses_when_nothing_is_staged(tmp_path, monkeypatch):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    responses = iter([(0, "", ""), (0, "", "")])
    monkeypatch.setattr(acts, "_run", lambda *a, **k: next(responses))

    result = commit_fix({"run_id": "sb-145", "declared": ["src/a.py"]})

    assert result["ok"] is False
    assert "nothing staged" in result["error"]


def test_suite_command_is_none_when_the_repo_has_no_suite(tmp_path):
    assert suite_command(tmp_path) is None


def test_dispatch_environment_stamps_unspoofable_identity():
    env = environment(
        {
            "run_id": "r1",
            "shard_id": "a",
            "stage_id": "s1",
            "shard_count": 2,
            "round": 3,
            "declared": ["src/a.py"],
        },
        {"PATH": "/usr/bin"},
    )
    assert env["SIGNALBOX_SHARD_ID"] == "a"
    assert env["SIGNALBOX_ROUND"] == "3"
    assert json.loads(env["SIGNALBOX_DECLARED"]) == ["src/a.py"]
    assert env["PATH"] == "/usr/bin"


def test_dispatch_environment_defaults_round_for_a_first_pass():
    assert environment({"run_id": "r1"}, {})["SIGNALBOX_ROUND"] == "1"


def test_pending_marker_is_named_for_the_shard_the_reaper_will_report():
    assert pending_path("shard", {"run_id": "r1", "shard_id": "a"}).name == "shard-r1-a.json"
    # A missing half renders "unknown" rather than quietly borrowing the other,
    # so an unaddressable marker is visible instead of colliding with a real one.
    assert pending_path("shard", {"shard_id": "a"}).name == "shard-unknown-a.json"
    assert pending_path("shard", {"run_id": "r1"}).name == "shard-r1-unknown.json"


def test_fixer_session_round_trips_outside_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))

    path = record_session("r1", "s1-fix", "codex", "session-123")

    assert path == tmp_path / "sessions" / "r1" / "session-s1-fix.json"
    assert read_session("r1", "s1-fix") == {
        "runner": "codex",
        "session_id": "session-123",
    }


def test_fixer_sessions_are_isolated_by_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))

    record_session("r1", "s1-store", "codex", "session-r1")
    record_session("r2", "s1-store", "claude", "session-r2")

    assert read_session("r1", "s1-store") == {
        "runner": "codex",
        "session_id": "session-r1",
    }
    assert read_session("r2", "s1-store") == {
        "runner": "claude",
        "session_id": "session-r2",
    }


def test_missing_or_unparsable_fixer_session_is_a_cold_start(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    assert read_session("r1", "missing") is None

    path = tmp_path / "sessions" / "r1" / "session-broken.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert read_session("r1", "broken") is None

    path.write_text(json.dumps({"runner": "codex"}))
    assert read_session("r1", "broken") is None


def test_clear_session_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    record_session("r1", "s1-fix", "claude", "session-456")

    assert clear_session("r1", "s1-fix") is True
    assert clear_session("r1", "s1-fix") is False
    assert read_session("r1", "s1-fix") is None


def test_submission_clears_pending_but_preserves_session_for_review(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    payload = {
        "event": "shard.submitted",
        "run_id": "r1",
        "shard_id": "s1-fix",
    }
    marker = pending_path("shard", payload)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps(payload))
    record_session("r1", "s1-fix", "codex", "session-123")

    assert clear_pending("shard", payload) is True
    assert read_session("r1", "s1-fix") == {
        "runner": "codex",
        "session_id": "session-123",
    }


@pytest.mark.parametrize("event", ["shard.approved", "scope.violated", "pr.merged"])
def test_terminal_event_retires_fixer_session(tmp_path, monkeypatch, event):
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    payload = {"event": event, "run_id": "r1", "shard_id": "s1-fix"}
    record_session("r1", "s1-fix", "codex", "session-123")

    clear_pending("shard", payload)

    assert read_session("r1", "s1-fix") is None


def test_reaping_a_silent_shard_also_retires_its_session(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    payload = {"run_id": "r1", "shard_id": "s1-fix"}
    marker = pending_path("shard", payload)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps(payload))
    record_session("r1", "s1-fix", "codex", "session-123")

    assert json.loads(reap("shard", stale_minutes=0))["outcome"] == "silent"
    assert read_session("r1", "s1-fix") is None


def test_a_missing_worktree_raises_rather_than_defaulting(tmp_path, monkeypatch):
    """The bug this prevents: an agent judged the wrong repository, confidently.

    run.built once carried no run_id, so the worktree resolved to
    .../worktrees/unknown, the path did not exist, and the assessor silently
    fell back to the engine's working directory and reviewed that instead.
    """
    from signalbox.paths import WorktreeMissing, require_worktree

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    with pytest.raises(WorktreeMissing):
        require_worktree({"run_id": "nope"})


def test_run_suite_distinguishes_no_worktree_from_no_suite(tmp_path, monkeypatch):
    from signalbox.acts import run_suite

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    result = run_suite({"run_id": "nope"})
    assert result["ran"] is False
    assert result["ok"] is False
    assert result["errored"] is False
    assert result["passed"] == result["failed"] == 0
    assert "no worktree" in result["reason"]
    assert not (tmp_path / "locks").exists()


def test_run_suite_contract_for_pass_and_failure(tmp_path, monkeypatch):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    root = tmp_path / "worktrees" / "sb-suite"
    root.mkdir(parents=True)
    monkeypatch.setattr(acts, "suite_command", lambda root: ["runner", "test"])

    monkeypatch.setattr(acts, "_run", lambda *a, **k: (
        0, "================ 267 passed in 1.2s ================", "",
    ))
    passed = acts.run_suite({"run_id": "sb-suite", "issue": 66})
    assert {key: passed[key] for key in ("ran", "ok", "passed", "failed", "errored")} == {
        "ran": True, "ok": True, "passed": 267, "failed": 0, "errored": False,
    }
    assert passed["issue"] == 66

    monkeypatch.setattr(acts, "_run", lambda *a, **k: (
        1, "", "=============== 3 failed, 264 passed in 1.2s ===============",
    ))
    failed = acts.run_suite({"run_id": "sb-suite", "issue": 66})
    assert {key: failed[key] for key in ("ran", "ok", "passed", "failed", "errored")} == {
        "ran": True, "ok": False, "passed": 264, "failed": 3, "errored": False,
    }
    assert failed["run_id"] == "sb-suite"


@pytest.mark.parametrize("outcome", ["success", "oserror", "unexpected", "timeout"])
def test_run_suite_holds_and_releases_the_global_lock(tmp_path, monkeypatch, outcome):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    root = tmp_path / "worktrees" / "sb-suite"
    root.mkdir(parents=True)
    command = ["runner", "test"]
    monkeypatch.setattr(acts, "suite_command", lambda root: command)
    lock_path = tmp_path / "locks" / "suite.lock"
    invoked = []

    def fake_run(cmd, cwd=None, timeout=120):
        invoked.append((cmd, cwd, timeout))
        with lock_path.open("a") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if outcome == "oserror":
            raise OSError("runner unavailable")
        if outcome == "unexpected":
            raise RuntimeError("unexpected failure")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout)
        return 0, "1 passed", ""

    monkeypatch.setattr(acts, "_run", fake_run)
    if outcome == "unexpected":
        with pytest.raises(RuntimeError, match="unexpected failure"):
            acts.run_suite({"run_id": "sb-suite"})
    else:
        result = acts.run_suite({"run_id": "sb-suite"})
        assert result["ok"] is (outcome == "success")
        assert result["errored"] is (outcome != "success")
        if outcome == "timeout":
            assert result["ran"] is False
            assert result["passed"] == result["failed"] == 0
            assert result["reason"] == "the suite did not finish within 30 minutes"
            assert result["run_id"] == "sb-suite"

    assert [(cmd, cwd) for cmd, cwd, _ in invoked] == [(command, str(root))]
    assert 1790 < invoked[0][2] <= 1800
    with lock_path.open("a") as contender:
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender, fcntl.LOCK_UN)


def test_suite_counts_accumulate_cargo_test_binaries():
    from signalbox.acts import suite_counts

    output = "\n".join([
        "test result: ok. 12 passed; 0 failed; 1 ignored",
        "test result: FAILED. 8 passed; 2 failed; 0 ignored",
    ])
    assert suite_counts(output) == (20, 2)


def test_run_suite_contract_for_absent_and_uninvokable_suite(tmp_path, monkeypatch):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    root = tmp_path / "worktrees" / "sb-suite"
    root.mkdir(parents=True)
    monkeypatch.setattr(acts, "suite_command", lambda root: None)

    absent = acts.run_suite({"run_id": "sb-suite"})
    assert {key: absent[key] for key in ("ran", "ok", "passed", "failed", "errored")} == {
        "ran": False, "ok": False, "passed": 0, "failed": 0, "errored": False,
    }

    (root / "Cargo.toml").write_text("")
    unavailable = acts.run_suite({"run_id": "sb-suite"})
    assert unavailable["ran"] is False
    assert unavailable["ok"] is False
    assert unavailable["errored"] is True
    assert unavailable["passed"] == unavailable["failed"] == 0
    assert not (tmp_path / "locks").exists()


def test_a_uv_managed_project_is_reached_through_its_manager(tmp_path):
    """The defect this prevents: signalbox could not detect its own suite.

    102 tests live behind `uv run pytest`; bare `pytest` is on no PATH here. The
    old check asked whether the binary existed, answered no, and published
    "no suite detected" — so the assessor refused to clear the gate for a run
    whose suite never ran, and the repository became silently ungateable.
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert suite_command(tmp_path) == ["uv", "run", "pytest", "-q"]


def test_detection_falls_through_to_a_marker_whose_runner_exists(tmp_path, monkeypatch):
    import shutil as shutil_module

    from signalbox import acts

    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "package.json").write_text("{}")
    # Neither Python runner installed; the JS one is.
    monkeypatch.setattr(
        acts.shutil, "which", lambda name: None if name in {"uv", "pytest"} else "/usr/bin/" + name
    )
    assert suite_command(tmp_path) == ["pnpm", "test"]
    assert shutil_module.which  # the real module is untouched


def test_a_marker_with_no_reachable_runner_is_still_none(tmp_path, monkeypatch):
    from signalbox import acts

    (tmp_path / "Cargo.toml").write_text("")
    monkeypatch.setattr(acts.shutil, "which", lambda name: None)
    assert suite_command(tmp_path) is None


def test_every_marker_offers_more_than_one_runner():
    """A single hard-coded runner per ecosystem is what caused the defect."""
    from signalbox.acts import SUITE_COMMANDS

    for marker, candidates in SUITE_COMMANDS:
        assert len(candidates) >= 2, f"{marker} has no fallback runner"


# ── releasing the workspace ──────────────────────────────────────────────────


def test_source_repo_is_the_parent_of_a_dot_git_common_dir():
    assert source_repo("/home/u/code/proj/.git") == Path("/home/u/code/proj")


def test_source_repo_of_a_bare_repository_is_the_common_dir_itself():
    """`--git-common-dir` does not always end in `.git`, and stripping blindly
    would hand `git worktree remove` the parent of a bare repo."""
    assert source_repo("/srv/git/proj.git") == Path("/srv/git/proj.git")


def _release(tmp_path, monkeypatch, responses, *, exists=True):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    if exists:
        (tmp_path / "worktrees" / "sb-69").mkdir(parents=True)
    calls = []

    def fake_run(cmd, cwd=None, timeout=120):
        calls.append((cmd, cwd))
        return responses.pop(0)

    monkeypatch.setattr(acts, "_run", fake_run)
    return acts.release_workspace({"run_id": "sb-69"}), calls


def test_release_removes_the_worktree_from_the_repository_that_owns_it(tmp_path, monkeypatch):
    """A worktree cannot remove itself, so the act must run against the source."""
    result, calls = _release(tmp_path, monkeypatch, [
        (0, "/home/u/code/proj/.git", ""),
        (0, "", ""),
        (0, "", ""),
    ])
    assert result["ok"] is True
    assert result["released"] is True
    assert result["branch"] == "signalbox/run-sb-69"

    locate, remove, prune = calls
    assert locate[0][:2] == ["git", "rev-parse"]
    assert locate[1] == str(tmp_path / "worktrees" / "sb-69")
    assert remove[0] == ["git", "worktree", "remove", "--force",
                         str(tmp_path / "worktrees" / "sb-69")]
    assert remove[1] == "/home/u/code/proj"
    assert prune[0] == ["git", "branch", "-D", "signalbox/run-sb-69"]
    assert prune[1] == "/home/u/code/proj"


def test_release_of_an_absent_worktree_is_not_a_failure(tmp_path, monkeypatch):
    """The state being asserted already holds, and git is never called."""
    result, calls = _release(tmp_path, monkeypatch, [], exists=False)
    assert result["ok"] is True
    assert result["released"] is False
    assert calls == []


def test_release_build_paths_resolve_under_state_without_creating_dirs(tmp_path, monkeypatch):
    from signalbox.paths import build_target_dir, run_tmp_dir

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    assert build_target_dir() == tmp_path / "target"
    assert run_tmp_dir("sb-69") == tmp_path / "tmp" / "sb-69"
    assert not build_target_dir().exists()
    assert not run_tmp_dir("sb-69").exists()


@pytest.mark.parametrize("exists", [True, False])
def test_release_removes_run_tmp_with_or_without_worktree(tmp_path, monkeypatch, exists):
    tmp = tmp_path / "tmp" / "sb-69"
    nested = tmp / "nested"
    nested.mkdir(parents=True)
    (nested / "build-output").write_text("temporary build output")
    other_run = tmp_path / "tmp" / "other-run"
    other_run.mkdir()
    responses = [
        (0, "/home/u/code/proj/.git", ""),
        (0, "", ""),
        (0, "", ""),
    ] if exists else []

    result, calls = _release(tmp_path, monkeypatch, responses, exists=exists)

    assert result["ok"] is True
    assert result["released"] is exists
    assert not tmp.exists()
    assert other_run.is_dir()
    assert len(calls) == (3 if exists else 0)


@pytest.mark.parametrize("exists", [True, False])
def test_release_missing_run_tmp_is_not_an_error(tmp_path, monkeypatch, exists):
    responses = [
        (0, "/home/u/code/proj/.git", ""),
        (0, "", ""),
        (0, "", ""),
    ] if exists else []

    result, _ = _release(tmp_path, monkeypatch, responses, exists=exists)

    assert result["ok"] is True
    assert result["released"] is exists
    assert not (tmp_path / "tmp" / "sb-69").exists()


def test_a_worktree_that_will_not_be_removed_is_a_refusal(tmp_path, monkeypatch):
    result, _ = _release(tmp_path, monkeypatch, [
        (0, "/home/u/code/proj/.git", ""),
        (1, "", "fatal: working trees containing modified files"),
    ])
    assert result["ok"] is False
    assert "modified files" in result["error"]


def test_a_surviving_local_branch_does_not_unrelease_a_removed_worktree(tmp_path, monkeypatch):
    """The worktree is already gone; calling that a failure would be a lie."""
    result, _ = _release(tmp_path, monkeypatch, [
        (0, "/home/u/code/proj/.git", ""),
        (0, "", ""),
        (1, "", "error: branch 'signalbox/run-sb-69' not found"),
    ])
    assert result["ok"] is True
    assert result["released"] is True
    assert "not found" in result["warning"]


def test_release_carries_run_identity_through_to_the_verdict(tmp_path, monkeypatch):
    """The routers select on .ok, but the register still needs to name the run."""
    result, _ = _release(tmp_path, monkeypatch, [
        (0, "/home/u/code/proj/.git", ""),
        (0, "", ""),
        (0, "", ""),
    ])
    assert result["run_id"] == "sb-69"


@pytest.mark.parametrize("change,scope", [
    ("clients/typescript/src/index.ts", "diff"),
    ("Cargo.toml", "full"), ("Cargo.lock", "full"), ("", "full"),
    ("unowned/readme.md", "full"),
])
def test_suite_diff_selection(tmp_path, monkeypatch, change, scope):
    from signalbox import acts
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    root = tmp_path / "worktrees" / "scoped"
    client = root / "clients/typescript"
    client.mkdir(parents=True)
    (root / "Cargo.toml").write_text("[workspace]")
    (client / "package.json").write_text('{"scripts":{"test":"vitest"}}')
    monkeypatch.setattr(acts.shutil, "which", lambda name: "/bin/" + name)
    calls = []

    def run(cmd, cwd=None, timeout=120):
        calls.append((cmd, cwd))
        if cmd[:2] == ["git", "diff"]:
            assert cmd[-1] == "rebased..HEAD"
            return 0, change, ""
        if cmd[:2] == ["cargo", "metadata"]:
            return 1, "", "no owner"
        return 0, "3 passed", ""

    monkeypatch.setattr(acts, "_run", run)
    result = acts.run_suite({"run_id": "scoped", "base_sha": "old", "rebased_onto_sha": "rebased"})
    assert result["suite_scope"] == scope
    assert result["ok"] is True
    assert result["passed"] == 3
    assert result["command"] == ("pnpm test" if scope == "diff" else "cargo nextest run")
    assert result["suites"] == [{"root": "clients/typescript" if scope == "diff" else ".",
        "command": result["command"], "exit_code": 0, "passed": 3, "failed": 0}]
    assert calls[-1][1] == str(client if scope == "diff" else root)
    if scope == "full":
        assert result["fallback_reason"]


@pytest.mark.parametrize("nextest", [True, False])
def test_cargo_diff_includes_reverse_workspace_dependencies(tmp_path, monkeypatch, nextest):
    from signalbox import acts
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    root = tmp_path / "worktrees/cargo"
    for name in ("a", "b", "c"):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "Cargo.toml").write_text("[package]")
    (root / "Cargo.toml").write_text("[workspace]")
    meta = {"workspace_root": str(root), "workspace_members": ["a", "b", "c"],
        "packages": [{"id": n, "name": n, "manifest_path": str(root / n / "Cargo.toml")}
                     for n in ("a", "b", "c")],
        "resolve": {"nodes": [{"id": "a", "dependencies": []},
            {"id": "b", "dependencies": ["a"]}, {"id": "c", "dependencies": []}]}}
    monkeypatch.setattr(acts.shutil, "which", lambda n: None if n == "cargo-nextest" and not nextest else n)
    calls = []

    def run(cmd, cwd=None, timeout=120):
        calls.append((cmd, cwd))
        if cmd[:2] == ["git", "diff"]:
            return 0, "a/Cargo.toml\na/src/lib.rs", ""
        if cmd[:2] == ["cargo", "metadata"]:
            return 0, json.dumps(meta), ""
        return 0, "test result: ok. 4 passed; 0 failed", ""

    monkeypatch.setattr(acts, "_run", run)
    result = acts.run_suite({"run_id": "cargo", "base_sha": "base"})
    expected = ["cargo", "nextest", "run"] if nextest else ["cargo", "test"]
    assert calls[-1] == (expected + ["-p", "a", "-p", "b"], str(root))
    assert result["suite_scope"] == "diff"
    assert len(result["suites"]) == 1


def test_diff_package_without_test_is_unmapped(tmp_path, monkeypatch):
    from signalbox import acts
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    root = tmp_path / "worktrees/absent"
    (root / "client").mkdir(parents=True)
    (root / "client/package.json").write_text("{}")
    monkeypatch.setattr(acts, "_run", lambda *a, **k: (0, "client/index.ts", ""))
    result = acts.run_suite({"run_id": "absent", "base_sha": "base"})
    assert result["changed_paths_unmapped"] == ["client/index.ts"]
    assert result["ran"] is False
    assert result["ok"] is False
    assert result["errored"] is True
    assert result["reason"] == "no suite covers the change"


@pytest.mark.parametrize("mode", ["failure", "timeout", "budget"])
def test_multiple_diff_suites_share_budget_and_report_results(tmp_path, monkeypatch, mode):
    from signalbox import acts
    import subprocess
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    root = tmp_path / "worktrees/multi"
    for name in ("one", "two"):
        (root / name).mkdir(parents=True)
        (root / name / "pyproject.toml").write_text("")
    monkeypatch.setattr(acts.shutil, "which", lambda n: n)
    elapsed = [0]
    monkeypatch.setattr(acts.time, "monotonic", lambda: elapsed[0])
    timeouts = []

    def run(cmd, cwd=None, timeout=120):
        if cmd[0] == "git":
            return 0, "one/a.py\ntwo/b.py", ""
        timeouts.append(timeout)
        if len(timeouts) == 1:
            elapsed[0] = 1801 if mode == "budget" else 100
            return 0, "2 passed", ""
        if mode == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout)
        return 7, "1 failed, 3 passed", ""

    monkeypatch.setattr(acts, "_run", run)
    result = acts.run_suite({"run_id": "multi", "base_sha": "base"})
    assert result["ran"] is True
    assert result["ok"] is False
    assert result["suite_scope"] == "diff"
    assert timeouts[0] == 1800
    if mode == "failure":
        assert result["command"] == "uv run pytest -q; uv run pytest -q"
        assert [s["root"] for s in result["suites"]] == ["one", "two"]
        assert (result["passed"], result["failed"], result["exit_code"]) == (5, 1, 7)
        assert result["errored"] is False
    else:
        assert result["errored"] is True
        assert result["exit_code"] != 0
        assert len(result["suites"]) == 1
    if mode != "budget":
        assert timeouts[1] == 1700


@pytest.mark.parametrize("failure", ["diff", "metadata"])
def test_suite_discovery_failure_falls_back(tmp_path, monkeypatch, failure):
    from signalbox import acts
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    root = tmp_path / "worktrees/discovery"
    root.mkdir(parents=True)
    (root / "Cargo.toml").write_text("")
    monkeypatch.setattr(acts.shutil, "which", lambda n: n)

    def run(cmd, **kwargs):
        if cmd[0] == "git":
            return (1, "", "git failed") if failure == "diff" else (0, "src/lib.rs", "")
        if cmd[:2] == ["cargo", "metadata"]:
            return 1, "", "metadata failed"
        return 0, "2 passed", ""

    monkeypatch.setattr(acts, "_run", run)
    result = acts.run_suite({"run_id": "discovery", "base_sha": "base"})
    assert result["suite_scope"] == "full"
    assert failure in result["fallback_reason"]
    assert result["ok"] is True


@pytest.mark.parametrize("bad", ["..", ".", "../x", "a/b", "/abs", "", "-x"])
def test_run_tmp_dir_refuses_a_run_id_that_is_not_one_plain_segment(bad):
    from signalbox.paths import run_tmp_dir
    with pytest.raises(ValueError):
        run_tmp_dir(bad)


@pytest.mark.parametrize("root_runner", [False, True])
def test_mapped_and_unmapped_changes_keep_selected_suites(tmp_path, monkeypatch, root_runner):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    root = tmp_path / "worktrees/mixed"
    client = root / "client"
    client.mkdir(parents=True)
    (client / "package.json").write_text('{"scripts":{"test":"vitest"}}')
    # A package without a test script is unmapped, but still has a root runner.
    if root_runner:
        (root / "package.json").write_text("{}")
    monkeypatch.setattr(acts.shutil, "which", lambda name: name)
    calls = []

    def run(cmd, cwd=None, timeout=120):
        if cmd[:2] == ["git", "diff"]:
            return 0, "client/index.ts\ndocs/readme.md", ""
        calls.append((cmd, cwd))
        return 0, "2 passed", ""

    monkeypatch.setattr(acts, "_run", run)
    result = acts.run_suite({"run_id": "mixed", "base_sha": "base"})
    expected = [(["pnpm", "test"], str(client))]
    if root_runner:
        expected.append((["pnpm", "test"], str(root)))
    assert calls == expected
    assert result["changed_paths_unmapped"] == ["docs/readme.md"]
    assert result["ran"] is result["ok"] is True
    # Without a root runner the readme is untested: the suites ran, but the
    # run goes to a person rather than being eligible for clear.
    assert result["errored"] is (not root_runner)
    if not root_runner:
        assert result["reason"] == "no suite covers these changed paths: docs/readme.md"
    assert result["suite_scope"] == "diff"
    assert len(result["suites"]) == len(expected)


@pytest.mark.parametrize("change,code,reason", [
    ("Makefile", 0, "no suite covers the change"),
    ("", 0, "no suite detected"),
    ("", 1, "no suite detected"),
])
def test_diff_fallback_without_root_runner(tmp_path, monkeypatch, change, code, reason):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    root = tmp_path / "worktrees/uncovered"
    root.mkdir(parents=True)
    calls = []

    def run(cmd, cwd=None, timeout=120):
        calls.append(cmd)
        assert cmd[:2] == ["git", "diff"]
        return code, change, "git failed" if code else ""

    monkeypatch.setattr(acts, "_run", run)
    result = acts.run_suite({"run_id": "uncovered", "base_sha": "base"})
    assert result["errored"] is bool(change)
    assert result["reason"] == reason
    assert result["ran"] is result["ok"] is False
    assert result["suites"] == []
    assert len(calls) == 1
    assert not (tmp_path / "locks").exists()


@pytest.mark.parametrize("mapped", [False, True])
def test_unmapped_changes_run_root_suite_once(tmp_path, monkeypatch, mapped):
    from signalbox import acts

    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    root = tmp_path / "worktrees/root-suite"
    root.mkdir(parents=True)
    (root / "Cargo.toml").write_text("[workspace]")
    monkeypatch.setattr(acts.shutil, "which", lambda name: name)
    meta = {"workspace_root": str(root), "workspace_members": ["root"],
        "packages": [{"id": "root", "name": "root", "manifest_path": str(root / "member/Cargo.toml")}]
                    if mapped else [],
        "resolve": {"nodes": [{"id": "root", "dependencies": []}]}}
    calls = []

    def run(cmd, cwd=None, timeout=120):
        if cmd[:2] == ["git", "diff"]:
            return 0, "member/src/lib.rs\ndocs/readme.md" if mapped else "docs/readme.md", ""
        if cmd[:2] == ["cargo", "metadata"]:
            return 0, json.dumps(meta), ""
        calls.append((cmd, cwd))
        return 0, "3 passed", ""

    monkeypatch.setattr(acts, "_run", run)
    result = acts.run_suite({"run_id": "root-suite", "base_sha": "base"})
    expected = ["cargo", "nextest", "run"] + (["-p", "root"] if mapped else [])
    assert calls == [(expected, str(root))]
    assert result["changed_paths_unmapped"] == ["docs/readme.md"]
    assert result["ran"] is result["ok"] is True
    assert result["errored"] is False
    assert len(result["suites"]) == 1
