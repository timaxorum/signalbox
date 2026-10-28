"""Repository-scoped PR waits and legacy compatibility (#467)."""

import json
import os

import pytest

from signalbox import acts
from signalbox.dispatch import pending_path


def test_same_pr_number_in_two_repos_keeps_two_markers(tmp_path, monkeypatch):
    """#467: equal PR numbers in different code repos cannot overwrite a run."""
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    a = acts.mark_pending("pr", {
        "pr": 7, "repo": "issues/tracker", "code_repo": "acme/a", "run_id": "a",
    })
    b = acts.mark_pending("pr", {
        "pr": 7, "repo": "issues/tracker", "code_repo": "acme/b", "run_id": "b",
    })
    assert {p.name for p in a.parent.glob("pr-*.json")} == {
        "pr-acme__a-7.json", "pr-acme__b-7.json",
    }
    assert acts.rehydrate("pr", {"pr": 7, "code_repo": "acme/a"})["run_id"] == "a"
    assert acts.clear_pending("pr", {"pr": 7, "code_repo": "acme/a"})
    assert b.exists()


def test_a_check_suite_resolves_by_repo_and_number(tmp_path, monkeypatch):
    """#467 and #434: a suite finds its run and preserves the issue repo."""
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    for repo, run_id, issue in [("acme/a", "a", 1), ("acme/b", "b", 2)]:
        acts.mark_pending("pr", {
            "pr": 7, "repo": "issues/tracker", "code_repo": repo,
            "run_id": run_id, "issue": issue, "base_sha": run_id + "-base",
        })
    result = acts.rehydrate("pr", {
        "pr": 7, "code_repo": "acme/b", "sha": "tested",
        "conclusion": "success", "check_runs_url": "https://api.github.com/check-runs",
    })
    assert (result["run_id"], result["issue"], result["base_sha"]) == ("b", 2, "b-base")
    assert result["repo"] == "issues/tracker"


def test_a_legacy_marker_is_read_for_its_own_repo_only(tmp_path, monkeypatch):
    """#467: legacy reads require matching repos and leave the old file intact."""
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    legacy = tmp_path / "pending" / "pr-7.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"pr": 7, "repo": "acme/a", "run_id": "old"}))
    assert acts.rehydrate("pr", {"pr": 7, "code_repo": "acme/a"})["run_id"] == "old"
    with pytest.raises(acts.PendingMissing):
        acts.rehydrate("pr", {"pr": 7, "code_repo": "acme/b"})
    assert not acts.clear_pending("pr", {"pr": 7, "repo": "acme/a"})
    assert legacy.exists()
    acts.mark_pending("pr", {"pr": 7, "code_repo": "acme/a", "run_id": "new"})
    assert acts.rehydrate("pr", {"pr": 7, "code_repo": "acme/a"})["run_id"] == "new"


@pytest.mark.parametrize("raw", ["invalid json", "[]", '{"repo":"issues/tracker"}'])
def test_an_incompatible_legacy_marker_is_missing(tmp_path, monkeypatch, raw):
    """#467: corrupt and cross-repo legacy markers cannot supply run identity."""
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    legacy = tmp_path / "pending" / "pr-7.json"
    legacy.parent.mkdir()
    legacy.write_text(raw)
    with pytest.raises(acts.PendingMissing):
        acts.rehydrate("pr", {"pr": 7, "code_repo": "acme/a"})
    assert legacy.read_text() == raw


def test_reap_still_ages_out_pr_markers(tmp_path, monkeypatch):
    """#467: both naming forms remain visible to the unchanged reaper."""
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    marker = acts.mark_pending("pr", {"pr": 7, "code_repo": "acme/a", "run_id": "new"})
    legacy = marker.parent / "pr-8.json"
    legacy.write_text(json.dumps({"pr": 8, "repo": "acme/a", "run_id": "old"}))
    old = marker.stat().st_mtime - 3600
    for path in (marker, legacy):
        os.utime(path, (old, old))
    results = [json.loads(line) for line in acts.reap("pr", 20).splitlines()]
    assert {result["run_id"] for result in results} == {"new", "old"}
    assert not marker.exists()
    assert not legacy.exists()


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/acme/widget/pull/7", "acme/widget"),
    ("", "existing/repo"),
])
def test_open_pr_records_code_repo_from_the_pr_url(tmp_path, monkeypatch, url, expected):
    """#467: the verified PR repo reaches the pending writer, overriding stale identity."""
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path))
    responses = iter([
        (0, "https://github.com/acme/widget/pull/7", ""),
        (0, json.dumps({
            "number": 7, "url": url, "baseRefName": "main",
            "headRefName": "signalbox/run-a", "state": "OPEN",
        }), ""),
    ])
    monkeypatch.setattr(acts, "_run", lambda *args, **kwargs: next(responses))
    result = acts.open_pr({
        "run_id": "a", "repo": "issues/tracker", "code_repo": "existing/repo",
        "issue": 467, "base_branch": "main",
    })
    assert result["ok"] is True
    assert result["code_repo"] == expected
    assert result["repo"] == "issues/tracker"
    assert pending_path("pr", result).name == f"pr-{expected.replace('/', '__')}-7.json"
