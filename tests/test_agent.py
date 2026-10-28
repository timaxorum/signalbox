"""Role-specific verdict handling preserves authoritative act observations."""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from signalbox import agent, paths
from signalbox.identity import project


@pytest.fixture
def notes_agent(monkeypatch, tmp_path):
    """Record model invocation and provenance for notes regressions (#452)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    calls, posted = [], []
    monkeypatch.setenv("SIGNALBOX_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("SIGNALBOX_RUNNER", "claude")
    monkeypatch.setenv("SIGNALBOX_VAULT", str(vault))
    monkeypatch.setattr(agent, "_workdir", lambda payload: str(tmp_path))
    completed = SimpleNamespace(returncode=0, stdout='{"outcome":"written"}', stderr="")
    monkeypatch.setattr(
        agent.subprocess, "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or completed,
    )
    monkeypatch.setattr(
        agent, "post_provenance",
        lambda *args, **kwargs: posted.append((args, kwargs)),
    )
    return SimpleNamespace(vault=vault, calls=calls, posted=posted, completed=completed)


@pytest.mark.parametrize(
    "suite_scope",
    [
        {"requires_human": True, "untested_paths": [".github/workflows/ci.yml"]},
        {"requires_human": False, "untested_paths": []},
        {"requires_human": True},
        {"untested_paths": ["pkg/uncovered.py"]},
        {},
    ],
)
def test_assess_preserves_only_inbound_suite_scope(notes_agent, suite_scope):
    """#462: models cannot clear, replace, or invent suite scope observations."""
    notes_agent.completed.stdout = json.dumps(
        {"decision": "clear", "requires_human": False, "untested_paths": ["invented"]}
    )
    payload = {"run_id": "r462", "ran": True, "ok": True, **suite_scope}

    verdict, code = agent.run("assess", payload)

    assert code == 0
    assert verdict == {"run_id": "r462", "decision": "clear", **suite_scope}
    assert len(notes_agent.calls) == 1
    assert len(notes_agent.posted) == 1


@pytest.mark.parametrize("dotdir", [False, True])
def test_empty_notes_vault_returns_identity_without_model(notes_agent, monkeypatch, dotdir):
    """#452: absent notes bypass the model, including metadata-only vaults."""
    if dotdir:
        metadata = notes_agent.vault / ".obsidian"
        metadata.mkdir()
        (metadata / "app.json").write_text("{}")
    monkeypatch.setattr(
        agent, "prompt_for",
        lambda *args: pytest.fail("an empty vault must not build a command"),
    )
    payload = {"run_id": "r452", "issue": 452, "base_branch": "main", "pr": 12,
               "outcome": "stale", "notes": ["stale"]}

    verdict, code = agent.run("plan-notes", payload)

    assert code == 0
    assert verdict == {"notes": [], "outcome": "empty-vault", **project(payload)}
    assert notes_agent.calls == []
    assert notes_agent.posted == []


@pytest.mark.parametrize("repaired", [False, True])
def test_model_empty_notes_vault_claim_is_invalid(notes_agent, repaired):
    """#452: even a repaired model verdict cannot claim the act's bypass."""
    nested = notes_agent.vault / "nested"
    nested.mkdir()
    (nested / "note.md").write_text("A note")
    stdout = json.dumps({"outcome": "empty-vault", "notes": [], "considered": ["note"]})
    notes_agent.completed.stdout = stdout[:-1] if repaired else stdout

    verdict, code = agent.run("plan-notes", {"run_id": "r452"})

    assert code == 0
    assert verdict["outcome"] == "invalid"
    assert "notes" not in verdict
    assert "considered" not in verdict
    assert "empty-vault is reserved for the act" in verdict["reason"]
    assert verdict["run_id"] == "r452"
    assert len(notes_agent.calls) == 1


@pytest.mark.parametrize("configured", [False, True])
def test_missing_notes_vault_still_halts(notes_agent, monkeypatch, configured):
    """#452: no configuration and nonexistent vaults remain errors."""
    if configured:
        monkeypatch.setenv("SIGNALBOX_VAULT", str(notes_agent.vault / "missing"))
    else:
        monkeypatch.delenv("SIGNALBOX_VAULT")
    with pytest.raises(paths.VaultMissing):
        agent.run("plan-notes", {"run_id": "r452"})
    assert notes_agent.calls == []
    assert notes_agent.posted == []


def test_write_notes_with_empty_vault_still_invokes_model(notes_agent):
    """#452: the empty-vault bypass belongs only to plan-notes."""
    verdict, code = agent.run("write-note", {"run_id": "r452"})
    assert code == 0
    assert verdict["outcome"] == "written"
    assert len(notes_agent.calls) == 1
    assert len(notes_agent.posted) == 1


def deny_notes_scan(monkeypatch, vault, nested):
    """Inject real scandir failures at either traversal depth (#452)."""
    denied = vault / "nested" if nested else vault
    if nested:
        denied.mkdir()
    scandir = paths.os.scandir

    def scan(path):
        if Path(path) == denied:
            raise PermissionError("vault scan denied")
        return scandir(path)

    monkeypatch.setattr(paths.os, "scandir", scan)


@pytest.mark.parametrize("nested", [False, True])
def test_notes_vault_scanner_propagates_permission_errors(notes_agent, monkeypatch, nested):
    """#452: errors at the root or mid-walk must never mean empty."""
    deny_notes_scan(monkeypatch, notes_agent.vault, nested)
    with pytest.raises(PermissionError, match="vault scan denied"):
        paths.vault_has_notes(notes_agent.vault)


@pytest.mark.parametrize("nested", [False, True])
def test_an_unreadable_notes_vault_halts_instead_of_reporting_empty(
    notes_agent, monkeypatch, nested
):
    """#452: the act turns traversal errors into the existing halt path."""
    deny_notes_scan(monkeypatch, notes_agent.vault, nested)
    with pytest.raises(paths.VaultMissing, match="could not be read") as caught:
        agent.run("plan-notes", {"run_id": "r452"})
    assert isinstance(caught.value.__cause__, PermissionError)
    assert notes_agent.calls == []
    assert notes_agent.posted == []


@pytest.mark.parametrize("nested", [False, True])
def test_unreadable_notes_vault_cli_emits_no_verdict(notes_agent, monkeypatch, capsys, nested):
    """#452: unreadable vaults exit one without publishing empty-vault."""
    deny_notes_scan(monkeypatch, notes_agent.vault, nested)
    monkeypatch.setattr(agent.sys, "stdin", io.StringIO('{"run_id":"r452"}'))
    assert agent.main(["plan-notes"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not be read" in captured.err
    assert len(captured.err.splitlines()) == 1
    assert notes_agent.calls == []
    assert notes_agent.posted == []
