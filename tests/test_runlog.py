"""Operator run diagnostics use the real store shape without writing it (#490)."""

from __future__ import annotations

import io
import json
import sqlite3
import threading
import time

from signalbox import cli, runlog


def _store(tmp_path, rows=()):
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, message_type TEXT, "
            "source TEXT, correlation_id TEXT, causation_id TEXT, timestamp_ms INTEGER, "
            "payload_json TEXT, metadata_json TEXT, created_at TEXT)"
        )
    for event_type, payload in rows:
        _insert(path, event_type, payload)
    return path


def _insert(path, event_type, payload):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO events (message_type, source, timestamp_ms, payload_json, "
            "metadata_json, created_at) VALUES (?, 'test', 1000, ?, '{}', 'now')",
            (event_type, json.dumps(payload)),
        )


def test_run_events_filters_by_run_and_after(tmp_path):
    path = _store(tmp_path, [
        ("run.requested", {"run_id": "r"}),
        ("run.completed", {"run_id": "other"}),
        ("plan.accepted", {"run_id": "r"}),
    ])
    before = path.read_bytes()
    events = runlog.run_events("r", path, after=1)
    assert events == [runlog.Event(3, "plan.accepted", 1000, {"run_id": "r"})]
    assert path.read_bytes() == before
    assert "reason=" in runlog.line(runlog.Event(1, "run.halted", 0, {"reason": "a\n b"}))
    assert runlog.line(runlog.Event(1, "run.halted", 0, {"reason": "a\n b"})).endswith("reason=a b")
    assert len(runlog.line(runlog.Event(1, "run.halted", 0, {"reason": "x" * 200})).split("reason=")[1]) == 160


def test_watch_exits_2_on_approval_and_prints_the_command(tmp_path):
    path = _store(tmp_path, [("approval.requested", {"run_id": "r", "reason": "review"})])
    out = io.StringIO()
    assert runlog.watch("r", path, out=out) == 2
    assert "approval.requested reason=review" in out.getvalue()
    assert out.getvalue().endswith("approve: signalbox approve r\n")


def test_watch_follows_new_events_until_completed(tmp_path):
    path = _store(tmp_path, [("run.requested", {"run_id": "r"})])
    def complete():
        time.sleep(0.05)
        _insert(path, "run.completed", {"run_id": "r"})
    thread = threading.Thread(target=complete)
    thread.start()
    out = io.StringIO()
    try:
        assert runlog.watch("r", path, poll=0.005, timeout=2, out=out) == 0
    finally:
        thread.join()
    assert out.getvalue().count("run.requested") == 1
    assert out.getvalue().count("run.completed") == 1


def test_watch_exits_3_on_halt_and_4_on_timeout(tmp_path):
    path = _store(tmp_path, [("run.halted", {"run_id": "r", "reason": "stopped"})])
    assert runlog.watch("r", path, out=io.StringIO()) == 3
    assert runlog.watch("other", path, poll=0.005, timeout=0.02, out=io.StringIO()) == 4


def test_watch_skips_non_milestones(tmp_path):
    path = _store(tmp_path, [(kind, {"run_id": "r"}) for kind in
                            ("model.invoked", "http.request", "run.completed")])
    out = io.StringIO()
    assert runlog.watch("r", path, out=out) == 0
    assert len(out.getvalue().splitlines()) == 1
    assert "run.completed" in out.getvalue()
    assert len(runlog.MILESTONES) == 19


def test_why_reports_objections_abandons_findings_and_gate(tmp_path):
    objection = {"claim": "unsafe", "evidence": "trace"}
    finding = {"severity": "low", "file": "x.py", "line": 5, "problem": "edge case"}
    gate = {"reason": "manual", "untested_paths": ["x.py"], "rationale": "needs review"}
    path = _store(tmp_path, [
        ("plan.rejected", {"run_id": "r", "objections": [{"claim": "old"}]}),
        ("plan.rejected", {"run_id": "r", "objections": [objection]}),
        ("shard.abandoned", {"run_id": "r", "shard_id": "s", "reason": "limit"}),
        ("shard.approved", {"run_id": "r", "shard_id": "s", "findings": [finding]}),
        ("run.halted", {"run_id": "r", "reason": "halted"}),
        ("pr.merge-failed", {"run_id": "r", "error": "conflict"}),
        ("approval.requested", {"run_id": "r", **gate}),
        ("http.request", {"run_id": "r"}),
    ])
    summary = runlog.why("r", path)
    assert summary == dict(status="approval.requested", objections=[objection],
                           abandoned=[{"shard_id": "s", "reason": "limit"}],
                           findings=[{"shard_id": "s", **finding}], gate=gate,
                           halt="halted", merge="conflict", next="signalbox approve r")
    rendered = runlog.render_why(summary)
    assert all(f"{key}:" in rendered for key in summary)
    assert "x" * 601 not in runlog.render_why({"halt": "x" * 700})
    assert cli.main(["why", "r", "--store", str(path), "--json"]) == 0


def test_why_drops_a_gate_once_granted(tmp_path):
    path = _store(tmp_path, [(kind, {"run_id": "r"}) for kind in
                            ("approval.requested", "approval.granted")])
    summary = runlog.why("r", path)
    assert summary["gate"] is None
    assert summary["next"] is None
    assert summary["status"] == "approval.granted"


def test_why_cli_on_an_unknown_run_exits_1(tmp_path, capsys):
    path = _store(tmp_path)
    assert cli.main(["why", "unknown", "--store", str(path)]) == 1
    assert capsys.readouterr().out == "no events for unknown\n"
    assert {"watch", "why"} <= cli.COMMANDS
    assert not {"watch", "why"} & cli.ACT_COMMANDS
    assert cli.main(["watch", "unknown", "--store", str(path), "--timeout", "0"]) == 4


def test_missing_store_is_no_events(tmp_path):
    path = tmp_path / "missing" / "events.db"
    assert runlog.run_events("r", path) == []
    assert runlog.watch("r", path, timeout=0.01, poll=0.002, out=io.StringIO()) == 4
    assert not path.exists()
    unreadable = tmp_path / "invalid.db"
    unreadable.write_text("not sqlite")
    assert runlog.run_events("r", unreadable) == []
