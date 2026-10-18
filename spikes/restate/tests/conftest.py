"""A real restate-server and a real service process, both killable.

The SDK's own test harness runs Restate in Docker and keeps the service in
process, which cannot show what these tests exist to show: what survives a
SIGKILL. So the fixture runs the server binary and the service as separate
processes and gives each test a fresh data directory.

Set RESTATE_SERVER_BIN to the server binary (`npm i @restatedev/restate-server`
puts one under node_modules/@restatedev/restate-server-linux-x64/bin/). Without
it the tests skip.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

import pytest

SPIKE_ROOT = Path(__file__).resolve().parents[1]
INGRESS = "http://127.0.0.1:8080"
ADMIN = "http://127.0.0.1:9070"
SERVICE = "127.0.0.1:9080"
LOCAL = {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}

# Talk to loopback directly, never through an outbound proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(method: str, url: str, body: object | None = None, timeout: float = 10) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    headers = {} if body is None else {"content-type": "application/json"}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _opener.open(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")}


def wait_until(predicate, timeout: float = 30, interval: float = 0.1, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


class Cluster:
    def __init__(self, tmp: Path, server_bin: str) -> None:
        self.tmp = tmp
        self.server_bin = server_bin
        self.data = tmp / "restate-data"
        self.ledger = tmp / "ledger.jsonl"
        self.ledger.touch()
        self.server: subprocess.Popen | None = None
        self.service: subprocess.Popen | None = None
        self.service_env: dict[str, str] = {}
        self.service_starts = 0

    # ── processes ────────────────────────────────────────────────────────────

    def start_server(self) -> None:
        log = (self.tmp / "server.log").open("a")
        self.server = subprocess.Popen(
            [self.server_bin, "--base-dir", str(self.data), "--bind-ip", "127.0.0.1",
             "--listen-mode", "tcp", "--no-logo", "--log-filter", "warn"],
            env={**os.environ, **LOCAL, "RESTATE_DISABLE_TELEMETRY": "true"},
            stdout=log, stderr=subprocess.STDOUT,
        )
        wait_until(lambda: self._healthy(), timeout=60, what="restate-server health")

    def _healthy(self) -> bool:
        try:
            return _request("GET", f"{ADMIN}/health", timeout=1)[0] == 200
        except OSError:
            return False

    def kill_server(self) -> None:
        assert self.server is not None
        self.server.send_signal(signal.SIGKILL)
        self.server.wait(timeout=10)

    def start_service(self, **env: str) -> None:
        self.service_env.update(env)
        log = (self.tmp / "service.log").open("a")
        self.service = subprocess.Popen(
            [sys.executable, str(SPIKE_ROOT / "serve.py"), SERVICE],
            cwd=SPIKE_ROOT,
            env={**os.environ, **LOCAL, "SPIKE_LEDGER": str(self.ledger),
                 "SPIKE_INGRESS": INGRESS, "SIGNALBOX_SPIKE_ACTS": "fake", **self.service_env},
            stdout=log, stderr=subprocess.STDOUT,
        )
        self.service_starts += 1
        wait_until(self._service_up, timeout=20, what="service to listen")

    def _service_up(self) -> bool:
        try:
            # Restate's discovery route answers any GET on the endpoint root path.
            _request("GET", f"http://{SERVICE}/discover", timeout=1)
            return True
        except OSError:
            return False

    def kill_service(self) -> None:
        assert self.service is not None
        if self.service.poll() is None:
            self.service.send_signal(signal.SIGKILL)
        self.service.wait(timeout=10)

    def register(self) -> None:
        status, body = _request("POST", f"{ADMIN}/deployments",
                                {"uri": f"http://{SERVICE}", "force": True})
        assert status in (200, 201), body

    def stop(self) -> None:
        for process in (self.service, self.server):
            if process and process.poll() is None:
                process.send_signal(signal.SIGKILL)
                process.wait(timeout=10)

    # ── the run ──────────────────────────────────────────────────────────────

    def start_run(self, run_id: str, sha: str = "s0") -> None:
        status, body = _request("POST", f"{INGRESS}/CiCycle/{run_id}/run/send",
                                {"run_id": run_id, "sha": sha, "pr": 7, "ci_round": 0})
        assert status in (200, 202), body

    def call(self, run_id: str, handler: str, body: object | None = None) -> dict:
        status, reply = _request("POST", f"{INGRESS}/CiCycle/{run_id}/{handler}", body, timeout=30)
        assert status == 200, (status, reply)
        return reply

    def checks(self, run_id: str, sha: str, conclusion: str) -> dict:
        return self.call(run_id, "checks_concluded", {"sha": sha, "conclusion": conclusion})

    def status(self, run_id: str) -> dict:
        return self.call(run_id, "status")

    def wait_phase(self, run_id: str, phase: str, timeout: float = 60, **match) -> dict:
        def reached():
            try:
                current = self.status(run_id)
            except (AssertionError, OSError):
                return None
            if current.get("phase") == phase and all(current.get(k) == v for k, v in match.items()):
                return current
            return None
        return wait_until(reached, timeout=timeout, what=f"{run_id} to reach {phase} {match}")

    def output(self, run_id: str, timeout: float = 60) -> dict:
        def done():
            status, body = _request("GET", f"{INGRESS}/restate/workflow/CiCycle/{run_id}/output")
            return body if status == 200 else None
        return wait_until(done, timeout=timeout, what=f"{run_id} output")

    def executions(self) -> Counter:
        lines = [json.loads(line) for line in self.ledger.read_text().splitlines() if line]
        return Counter(f"{entry['act']}:{entry['key']}" for entry in lines)

    def pids(self) -> set[int]:
        return {json.loads(line)["pid"] for line in self.ledger.read_text().splitlines() if line}


@pytest.fixture
def cluster(tmp_path: Path):
    server_bin = os.environ.get("RESTATE_SERVER_BIN") or shutil.which("restate-server")
    if not server_bin:
        pytest.skip("RESTATE_SERVER_BIN is not set and restate-server is not on PATH")
    for port in (8080, 9070, 9080):
        try:
            _request("GET", f"http://127.0.0.1:{port}/", timeout=0.3)
            pytest.fail(f"port {port} is already in use; stop the other Restate instance first")
        except OSError:
            pass
    cluster = Cluster(tmp_path, server_bin)
    cluster.start_server()
    yield cluster
    cluster.stop()


def boot(cluster: Cluster, **env: str) -> Cluster:
    cluster.start_service(**env)
    cluster.register()
    return cluster
