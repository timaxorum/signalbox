"""The acts the CI cycle performs, behind one seam.

`RealActs` is a thin adapter over what signalbox already has: the pure-ish
functions in `signalbox.acts`, and the `signalbox agent` / `signalbox dispatch`
CLIs the topology's exec-handlers run today. Nothing in signalbox changes.

`FakeActs` stands in for git, gh and the models so the durability properties
can be tested against a real Restate server in seconds. It writes every
execution to a ledger file, which is how a test proves a step ran once even
across process kills, and it can kill its own process mid-step.

Pick one with SIGNALBOX_SPIKE_ACTS=real|fake (default fake).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Protocol


class Acts(Protocol):
    def check_details(self, payload: dict) -> dict: ...
    def map_ci_findings(self, payload: dict) -> dict: ...
    def start_fix(self, payload: dict) -> dict: ...
    def review(self, payload: dict) -> dict: ...
    def commit_fix(self, payload: dict) -> dict: ...
    def rebase(self, payload: dict) -> dict: ...
    def run_suite(self, payload: dict) -> dict: ...
    def assess(self, payload: dict) -> dict: ...
    def push_branch(self, payload: dict) -> dict: ...
    def merge_pr(self, payload: dict) -> dict: ...
    def announce(self, payload: dict) -> dict: ...


def load_acts() -> Acts:
    kind = os.environ.get("SIGNALBOX_SPIKE_ACTS", "fake")
    if kind == "real":
        return RealActs()
    if kind == "fake":
        return FakeActs()
    raise ValueError(f"SIGNALBOX_SPIKE_ACTS must be real or fake, not {kind!r}")


# ── real ─────────────────────────────────────────────────────────────────────


def _agent(role: str, payload: dict, timeout: int) -> dict:
    """`signalbox agent <role>`, exactly as its exec-handler runs it."""
    completed = subprocess.run(
        ["signalbox", "agent", role],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        # Raising makes the step retry (RunOptions.max_attempts) and then
        # surface as a terminal error, which is the -e failure topic's job today.
        raise RuntimeError(f"signalbox agent {role} exited {completed.returncode}: "
                           f"{completed.stderr.strip()[-2000:]}")
    return {**payload, **json.loads(completed.stdout)}


class RealActs:
    def check_details(self, payload: dict) -> dict:
        from signalbox import acts
        return acts.check_details(payload)

    def map_ci_findings(self, payload: dict) -> dict:
        from signalbox import acts
        return acts.map_ci_findings(payload)

    def start_fix(self, payload: dict) -> dict:
        """Spawn the fixer and return at once; its announcement resolves the wait.

        A step can run twice if the process dies between finishing and being
        journaled, and a fixer must never be dispatched twice. The O_EXCL claim
        makes the second spawn a no-op.
        """
        from signalbox.paths import state_dir

        claim = (state_dir() / "restate-spike" / "fix-claims"
                 / f"{payload['run_id']}-{payload['ci_round']}-{payload['round']}")
        claim.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            return {**payload, "spawned": False}
        _spawn_detached(payload)
        return {**payload, "spawned": True}

    def review(self, payload: dict) -> dict:
        return _agent("review", payload, timeout=900)

    def commit_fix(self, payload: dict) -> dict:
        from signalbox import acts
        return acts.commit_fix(payload)

    def rebase(self, payload: dict) -> dict:
        return _agent("rebase", payload, timeout=900)

    def run_suite(self, payload: dict) -> dict:
        from signalbox import acts
        return acts.run_suite(payload)

    def assess(self, payload: dict) -> dict:
        return _agent("assess", payload, timeout=900)

    def push_branch(self, payload: dict) -> dict:
        from signalbox import acts
        return acts.push_branch(payload)

    def merge_pr(self, payload: dict) -> dict:
        from signalbox import acts
        return acts.merge_pr(payload)

    def announce(self, payload: dict) -> dict:
        from signalbox import emit
        status = emit.post({"event": payload["terminal"], **payload}, url=emit.control_url())
        return {"status": status}


def _spawn_detached(payload: dict) -> None:
    process = subprocess.Popen(
        ["signalbox", "dispatch", "--skill", "signalbox-fix"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload))
    process.stdin.close()


# ── fake ─────────────────────────────────────────────────────────────────────


class FakeActs:
    """Scripted stand-ins. Configuration comes from the environment so that a
    restarted service process picks up exactly the same script.

    SPIKE_LEDGER        jsonl file; one line per act execution
    SPIKE_SCRIPT        json: {"reviews": {"<ci>:<round>": verdict},
                               "fixes": {"<ci>:<round>": verdict},
                               "gates": {"<ci>": decision},
                               "review_seconds": {"<ci>:<round>": seconds},
                               "raise": ["<act>:<key>", ...]}
    SPIKE_CRASH_ONCE    "<act>:<key>" kills this process inside that act once,
                        after its side effect is recorded and before Restate
                        journals the result: the worst moment to die
    SPIKE_INGRESS       Restate ingress URL; the fake fixer announces through it
    """

    def __init__(self) -> None:
        self.ledger = Path(os.environ.get("SPIKE_LEDGER", "/dev/null"))
        self.script = json.loads(os.environ.get("SPIKE_SCRIPT", "{}"))
        self.crash_once = os.environ.get("SPIKE_CRASH_ONCE", "")
        self.ingress = os.environ.get("SPIKE_INGRESS", "http://127.0.0.1:8080")
        self.announce_fixes = os.environ.get("SPIKE_FAKE_FIXER", "1") == "1"

    def _record(self, act: str, key: str, payload: dict) -> None:
        with self.ledger.open("a") as handle:
            handle.write(json.dumps({"act": act, "key": key, "run_id": payload.get("run_id"),
                                     "pid": os.getpid()}) + "\n")
        target = f"{act}:{key}"
        if target in self.script.get("raise", []):
            raise RuntimeError(f"scripted failure in {target}")
        marker = Path(f"{self.ledger}.crashed")
        if self.crash_once == target and not marker.exists():
            marker.write_text(target)
            os._exit(17)

    def check_details(self, payload: dict) -> dict:
        self._record("check_details", str(payload.get("ci_round")), payload)
        return {**payload, "ok": True, "failed": [{"name": "unit-tests"}], "detail_ok": True}

    def map_ci_findings(self, payload: dict) -> dict:
        self._record("map_ci_findings", str(payload.get("ci_round")), payload)
        ci_round = int(payload.get("ci_round") or 0) + 1
        return {**payload, "ok": True, "ci_round": ci_round, "round": 1, "shard_id": "ci-fix",
                "ci_origin": "post-merge", "verdict": "changes_requested",
                "declared": ["src/app.py"],
                "findings": [{"source": "ci", "check": "unit-tests"}]}

    def start_fix(self, payload: dict) -> dict:
        key = f"{payload['ci_round']}:{payload['round']}"
        self._record("start_fix", key, payload)
        if self.announce_fixes:
            verdict = self.script.get("fixes", {}).get(key, "done")
            submission = {"run_id": payload["run_id"], "ci_round": payload["ci_round"],
                          "round": payload["round"], "verdict": verdict}
            threading.Thread(target=self._announce_fix, args=(payload["run_id"], submission),
                             daemon=True).start()
        return {**payload, "spawned": True}

    def _announce_fix(self, run_id: str, submission: dict) -> None:
        time.sleep(0.2)
        post(f"{self.ingress}/CiCycle/{run_id}/fix_submitted", submission)

    def review(self, payload: dict) -> dict:
        key = f"{payload['ci_round']}:{payload['round']}"
        self._record("review", key, payload)
        time.sleep(float(self.script.get("review_seconds", {}).get(key, 0)))
        verdict = self.script.get("reviews", {}).get(key, "approved")
        return {**payload, "verdict": verdict, "findings": [{"note": f"round {key}"}]}

    def commit_fix(self, payload: dict) -> dict:
        self._record("commit_fix", str(payload["ci_round"]), payload)
        return {**payload, "ok": True, "sha": f"commit-{payload['ci_round']}"}

    def rebase(self, payload: dict) -> dict:
        self._record("rebase", str(payload["ci_round"]), payload)
        return {**payload, "ok": True}

    def run_suite(self, payload: dict) -> dict:
        self._record("run_suite", str(payload["ci_round"]), payload)
        return {**payload, "ran": True, "ok": True, "passed": 10, "failed": 0, "errored": False}

    def assess(self, payload: dict) -> dict:
        self._record("assess", str(payload["ci_round"]), payload)
        decision = self.script.get("gates", {}).get(str(payload["ci_round"]), "clear")
        return {**payload, "decision": decision}

    def push_branch(self, payload: dict) -> dict:
        self._record("push_branch", str(payload["ci_round"]), payload)
        return {**payload, "ok": True, "sha": f"pushed-{payload['ci_round']}"}

    def merge_pr(self, payload: dict) -> dict:
        self._record("merge_pr", str(payload.get("ci_round") or 0), payload)
        return {**payload, "ok": True}

    def announce(self, payload: dict) -> dict:
        self._record("announce", payload["terminal"], payload)
        return {"status": 200}


def post(url: str, body: dict) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}
