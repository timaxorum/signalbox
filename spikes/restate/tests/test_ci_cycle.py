"""What the spike has to prove, against a real restate-server.

Each test names the topology incident it answers. The acts are fakes that
write a ledger, so "ran once" is a count across every process that ever served
the workflow, not a claim about one process's memory.
"""

from __future__ import annotations

import json
import time

from conftest import boot

# One red cycle, end to end: everything after the first red suite exactly once.
ONE_RED_CYCLE = {
    "check_details:0": 1, "map_ci_findings:0": 1,
    "start_fix:1:1": 1, "review:1:1": 1,
    "commit_fix:1": 1, "rebase:1": 1, "run_suite:1": 1, "assess:1": 1, "push_branch:1": 1,
    "merge_pr:1": 1, "announce:run.completed": 1,
}


def drive_one_red_cycle(cluster, run_id: str) -> dict:
    cluster.start_run(run_id)
    cluster.wait_phase(run_id, "awaiting-checks", sha="s0")
    cluster.checks(run_id, "s0", "failure")
    cluster.wait_phase(run_id, "awaiting-checks", sha="pushed-1")
    cluster.checks(run_id, "pushed-1", "success")
    return cluster.output(run_id)


def test_red_then_green_runs_every_act_once(cluster):
    boot(cluster)
    result = drive_one_red_cycle(cluster, "happy")
    assert result["terminal"] == "run.completed"
    assert cluster.executions() == ONE_RED_CYCLE


def test_reviewer_rejection_loops_inside_the_cycle(cluster):
    # #145: reviewer-requested edits stay inside the current hosted-CI cycle.
    boot(cluster, SPIKE_SCRIPT=json.dumps({"reviews": {"1:1": "changes_requested"}}))
    result = drive_one_red_cycle(cluster, "rejected-once")
    assert result["terminal"] == "run.completed"
    counts = cluster.executions()
    assert counts["start_fix:1:1"] == counts["start_fix:1:2"] == 1
    assert counts["review:1:1"] == counts["review:1:2"] == 1
    assert counts["commit_fix:1"] == 1


def test_service_killed_mid_step_resumes_without_redoing_finished_steps(cluster):
    # #139: a restart tears down a live run and strands it. Here the service
    # dies inside the review, after the review's side effect and before Restate
    # has journaled its result: the worst possible moment.
    boot(cluster, SPIKE_CRASH_ONCE="review:1:1")
    cluster.start_run("killed")
    cluster.wait_phase("killed", "awaiting-checks", sha="s0")
    cluster.checks("killed", "s0", "failure")

    cluster.service.wait(timeout=30)          # it killed itself mid-review
    assert cluster.service.returncode == 17
    cluster.start_service()                   # a restart; no re-registration, no babysitter

    cluster.wait_phase("killed", "awaiting-checks", sha="pushed-1")
    cluster.checks("killed", "pushed-1", "success")
    assert cluster.output("killed")["terminal"] == "run.completed"

    counts = cluster.executions()
    # The interrupted step ran again: at-least-once, which is why acts must be
    # idempotent (RealActs.start_fix's O_EXCL claim is the pattern).
    assert counts["review:1:1"] == 2
    # Nothing that finished before the crash ran again.
    assert counts["check_details:0"] == counts["map_ci_findings:0"] == counts["start_fix:1:1"] == 1
    assert {k: v for k, v in counts.items() if k != "review:1:1"} == {
        k: v for k, v in ONE_RED_CYCLE.items() if k != "review:1:1"}
    assert len(cluster.pids()) == 2


def test_whole_host_restart_while_waiting_on_ci(cluster):
    # The run is parked on hosted CI when both the server and the service die,
    # as on a host reboot. Nothing marks it pending; the journal is the marker.
    boot(cluster)
    cluster.start_run("rebooted")
    cluster.wait_phase("rebooted", "awaiting-checks", sha="s0")
    cluster.kill_service()
    cluster.kill_server()

    cluster.start_server()
    cluster.start_service()
    cluster.checks("rebooted", "s0", "failure")
    cluster.wait_phase("rebooted", "awaiting-checks", sha="pushed-1")
    cluster.checks("rebooted", "pushed-1", "success")
    assert cluster.output("rebooted")["terminal"] == "run.completed"
    assert cluster.executions() == ONE_RED_CYCLE


def test_webhook_arriving_before_the_wait_is_not_lost(cluster):
    # The topology needs mark-pr-pending, rehydrate and reap-prs because a
    # check_suite can conclude before anything is listening. A promise keyed by
    # sha can be resolved first and awaited later.
    boot(cluster)
    assert cluster.checks("early", "s0", "success")["resolved"] is True
    cluster.start_run("early")
    assert cluster.output("early")["terminal"] == "run.completed"
    assert cluster.executions() == {"merge_pr:0": 1, "announce:run.completed": 1}


def test_redelivered_and_stale_webhooks_change_nothing(cluster):
    boot(cluster)
    cluster.start_run("noisy")
    cluster.wait_phase("noisy", "awaiting-checks", sha="s0")
    assert cluster.checks("noisy", "s0", "cancelled")["resolved"] is False   # force-push leftovers
    assert cluster.checks("noisy", "s0", "failure")["resolved"] is True
    assert cluster.checks("noisy", "s0", "failure")["resolved"] is False     # redelivery
    cluster.wait_phase("noisy", "awaiting-checks", sha="pushed-1")
    assert cluster.checks("noisy", "s0", "success")["resolved"] is False     # superseded commit
    cluster.checks("noisy", "pushed-1", "success")
    assert cluster.output("noisy")["terminal"] == "run.completed"
    assert cluster.executions() == ONE_RED_CYCLE


def test_human_gate_holds_across_a_restart(cluster):
    boot(cluster, SPIKE_SCRIPT=json.dumps({"gates": {"1": "needs_human"}}))
    cluster.start_run("gated")
    cluster.wait_phase("gated", "awaiting-checks", sha="s0")
    cluster.checks("gated", "s0", "failure")
    cluster.wait_phase("gated", "awaiting-approval", ci_round=1)

    cluster.kill_service()
    cluster.start_service()
    assert cluster.status("gated")["phase"] == "awaiting-approval"
    assert "push_branch:1" not in cluster.executions()

    assert cluster.call("gated", "approve", {"by": "tim"})["resolved"] is True
    cluster.wait_phase("gated", "awaiting-checks", sha="pushed-1")
    cluster.checks("gated", "pushed-1", "success")
    assert cluster.output("gated")["terminal"] == "run.completed"


def test_reviewer_that_never_approves_halts_with_a_reason(cluster):
    # #145's inner loop used to cycle forever; the guard pair bounds it at five.
    never = {f"1:{n}": "changes_requested" for n in range(1, 6)}
    boot(cluster, SPIKE_SCRIPT=json.dumps({"reviews": never}))
    cluster.start_run("stubborn")
    cluster.wait_phase("stubborn", "awaiting-checks", sha="s0")
    cluster.checks("stubborn", "s0", "failure")
    result = cluster.output("stubborn")
    assert result["terminal"] == "run.halted"
    assert result["stranded_topic"] == "ci.changes-requested"
    counts = cluster.executions()
    assert counts["review:1:5"] == 1 and "review:1:6" not in counts
    assert counts["announce:run.halted"] == 1 and "commit_fix:1" not in counts


def test_ci_that_stays_red_halts_after_the_cycle_budget(cluster):
    # #145's outer loop: every approved fix leaves CI red.
    boot(cluster)
    cluster.start_run("red-forever")
    sha = "s0"
    for ci_round in range(1, 5):
        cluster.wait_phase("red-forever", "awaiting-checks", sha=sha)
        cluster.checks("red-forever", sha, "failure")
        sha = f"pushed-{ci_round}"
    cluster.wait_phase("red-forever", "awaiting-checks", sha=sha)
    cluster.checks("red-forever", sha, "failure")
    result = cluster.output("red-forever")
    assert result["terminal"] == "run.halted"
    assert result["stranded_topic"] == "ci.fix-ready"
    assert "unit-tests" in result["reason"]
    counts = cluster.executions()
    assert counts["push_branch:4"] == 1 and "start_fix:5:1" not in counts


def test_silent_fixer_times_out_instead_of_hanging(cluster):
    # The reaper's job (shard.silent), done by a durable timer in the run itself.
    boot(cluster, SPIKE_FAKE_FIXER="0", SIGNALBOX_FIX_TIMEOUT_S="2")
    cluster.start_run("silent")
    cluster.wait_phase("silent", "awaiting-checks", sha="s0")
    cluster.checks("silent", "s0", "failure")
    result = cluster.output("silent")
    assert result["terminal"] == "run.halted"
    assert result["stranded_topic"] == "shard.silent"


def test_an_act_that_keeps_raising_still_ends_in_a_terminal(cluster):
    # The -e failure topics (#110, #123): a node that errors must not strand
    # the run. Three attempts, then run.halted is announced.
    boot(cluster, SPIKE_SCRIPT=json.dumps({"raise": ["rebase:1"]}))
    cluster.start_run("broken")
    cluster.wait_phase("broken", "awaiting-checks", sha="s0")
    cluster.checks("broken", "s0", "failure")
    result = cluster.output("broken")
    assert result["terminal"] == "run.halted"
    assert result["stranded_topic"] == "act.failed"
    counts = cluster.executions()
    assert counts["rebase:1"] == 3
    assert counts["announce:run.halted"] == 1 and "run_suite:1" not in counts


def test_a_step_longer_than_the_abort_timeout_is_replayed(cluster):
    # The operational trap, measured rather than asserted: Restate cancels an
    # invocation that makes no journal progress for inactivity + abort timeout,
    # and the in-flight step runs again. A 15-minute review under the default
    # 10-minute abort would be dispatched twice. The workflow sets 45 minutes;
    # here both are shrunk so a 4-second review crosses the line.
    boot(cluster, SIGNALBOX_INACTIVITY_TIMEOUT_S="1", SIGNALBOX_ABORT_TIMEOUT_S="1",
         SPIKE_SCRIPT=json.dumps({"review_seconds": {"1:1": 4}}))
    cluster.start_run("slow")
    cluster.wait_phase("slow", "awaiting-checks", sha="s0")
    cluster.checks("slow", "s0", "failure")
    cluster.wait_phase("slow", "fixing", round=1)
    time.sleep(6)
    assert cluster.executions()["review:1:1"] >= 2
