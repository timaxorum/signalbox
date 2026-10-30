"""Explain and follow a run without hand-querying or changing its store (#490)."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import NamedTuple


class Event(NamedTuple):
    rowid: int
    type: str
    timestamp_ms: int
    payload: dict


MILESTONES = frozenset({
    "run.requested", "plan.accepted", "plan.rejected", "shard.approved",
    "shard.abandoned", "stage.merged", "run.built", "gate.cleared",
    "approval.requested", "approval.granted", "pr.opened", "checks.passed",
    "checks.failed", "pr.merge-failed", "remediation.requested", "pr.merged",
    "run.cancel-requested", "run.halted", "run.completed",
})


def run_events(run_id: str, store: Path, after: int = 0) -> list[Event]:
    try:
        uri = store.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            rows = connection.execute(
                "SELECT rowid, message_type, timestamp_ms, payload_json FROM events "
                "WHERE rowid > ? AND json_extract(payload_json, '$.run_id') = ? ORDER BY rowid",
                (after, run_id),
            )
            events = []
            for rowid, message_type, timestamp_ms, raw in rows:
                try:
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        events.append(Event(rowid, message_type, timestamp_ms, payload))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
            return events
    except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        return []


def line(event: Event) -> str:
    parts = [datetime.fromtimestamp(event.timestamp_ms / 1000).strftime("%H:%M:%S"), event.type]
    for key in ("shard_id", "verdict", "pr", "reason", "url"):
        if key in event.payload:
            value = event.payload[key]
            if key == "reason":
                value = " ".join(str(value).split())[:160]
            parts.append(f"{key}={value}")
    return " ".join(parts)


def watch(run_id, store, *, poll=2.0, timeout=None, out=sys.stdout) -> int:
    started = time.monotonic()
    after = 0
    while True:
        for event in run_events(run_id, store, after):
            after = event.rowid
            if event.type not in MILESTONES:
                continue
            print(line(event), file=out, flush=True)
            if event.type == "run.completed":
                return 0
            if event.type == "approval.requested":
                print(f"approve: signalbox approve {run_id}", file=out, flush=True)
                return 2
            if event.type == "run.halted":
                return 3
        remaining = None if timeout is None else timeout - (time.monotonic() - started)
        if remaining is not None and remaining <= 0:
            return 4
        time.sleep(max(0, poll if remaining is None else min(poll, remaining)))


def _fields(payload: dict, keys: tuple[str, ...]) -> dict:
    return {key: payload.get(key) for key in keys}


def why(run_id, store) -> dict:
    summary = dict(status=None, objections=[], abandoned=[], findings=[],
                   gate=None, halt=None, merge=None, next=None)
    for event in run_events(run_id, store):
        payload = event.payload
        if event.type in MILESTONES:
            summary["status"] = event.type
        if event.type == "plan.rejected":
            summary["objections"] = [
                _fields(item, ("claim", "evidence"))
                for item in payload.get("objections", []) if isinstance(item, dict)
            ]
        elif event.type == "shard.abandoned":
            summary["abandoned"].append(_fields(payload, ("shard_id", "reason")))
        elif event.type == "shard.approved":
            for finding in payload.get("findings", []):
                if isinstance(finding, dict):
                    summary["findings"].append({
                        **_fields(finding, ("severity", "file", "line", "problem")),
                        "shard_id": payload.get("shard_id"),
                    })
        elif event.type == "approval.requested":
            summary["gate"] = _fields(payload, ("reason", "untested_paths", "rationale"))
        elif event.type == "approval.granted":
            summary["gate"] = None
        elif event.type == "run.halted":
            summary["halt"] = payload.get("reason")
        elif event.type == "pr.merge-failed":
            summary["merge"] = payload.get("error")
    if summary["gate"] is not None:
        summary["next"] = f"signalbox approve {run_id}"
    return summary


def render_why(summary: dict) -> str:
    def display(value):
        if isinstance(value, dict):
            return ", ".join(f"{key}={display(field)}" for key, field in value.items())
        if isinstance(value, list):
            return ", ".join(display(field) for field in value)
        return " ".join(str(value).split())[:600]

    sections = []
    for key, value in summary.items():
        if value:
            items = value if isinstance(value, list) else [value]
            sections.append(f"{key}:\n" + "\n".join(f"  {display(item)}" for item in items))
    return "\n".join(sections)


def main(argv: list[str]) -> int:
    import argparse

    from signalbox.dashboard import DEFAULT_STORE

    command, *rest = argv
    parser = argparse.ArgumentParser(prog=f"signalbox {command}")
    parser.add_argument("run_id")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    if command == "watch":
        parser.add_argument("--poll", type=float, default=2.0)
        parser.add_argument("--timeout", type=float)
    else:
        parser.add_argument("--json", action="store_true")
    args = parser.parse_args(rest)
    if command == "watch":
        return watch(args.run_id, args.store, poll=args.poll, timeout=args.timeout, out=sys.stdout)
    if not run_events(args.run_id, args.store):
        print(f"no events for {args.run_id}")
        return 1
    summary = why(args.run_id, args.store)
    print(json.dumps(summary) if args.json else render_why(summary))
    return 0
