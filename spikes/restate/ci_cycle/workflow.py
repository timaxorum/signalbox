"""The post-merge CI cycle as one durable function.

This is the stretch of the topology that #145 and #151 kept stranding: a PR is
open, hosted CI reports, a red suite becomes findings, the fixer and reviewer
loop, the fix is committed, rebased, re-gated and re-pushed, and CI reports
again. In emergent.toml it is roughly forty handlers and routers whose only job
is to carry a run from one topic to the next; a topic nobody consumes, or a join
that already fired, leaves the run with no terminal.

Here the control flow is ordinary Python. Every exit is a `return` of either
`completed` or `halted`, so there is no topic for a run to fall through. Restate
journals each step's result, so a crash of this process, of the Restate server,
or of the host resumes from the last completed step instead of from nothing.

Waits are durable promises rather than processes:

  checks:<sha>              resolved by the check_suite webhook (`checks_concluded`)
  fix:<ci_round>:<round>    resolved by the fixer's own shard.submitted announcement
  approval:<ci_round>       resolved by the operator (`approve`)

A promise is keyed by what the resolver already knows, so a webhook that lands
before the workflow reaches its wait, or one for a superseded commit, is either
picked up later or ignored without any marker file.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any, Callable

import restate
from restate import RunOptions, TerminalError, WorkflowContext, WorkflowSharedContext

from .acts import load_acts

# The topology's own budgets, so the two implementations can be compared like
# for like: five hosted-CI cycles (guard-ci-rounds-*), five review rounds per
# cycle (guard-ci-review-rounds-*), and the reaper's silence thresholds.
MAX_CI_ROUNDS = 5
MAX_REVIEW_ROUNDS = 5
CHECKS_TIMEOUT = timedelta(seconds=float(os.environ.get("SIGNALBOX_CHECKS_TIMEOUT_S", 3600)))
FIX_TIMEOUT = timedelta(seconds=float(os.environ.get("SIGNALBOX_FIX_TIMEOUT_S", 2400)))

ACTS = load_acts()

ci_cycle = restate.Workflow(
    "CiCycle",
    # Steps run in a worker thread while the invocation stays open, so the
    # default 10-minute abort would cancel a 15-minute review and replay it. A
    # fixer is not a step at all: it is spawned, and the workflow suspends on its
    # promise, so the abort timeout only has to cover the longest synchronous
    # act (run-suite's 30 minutes).
    inactivity_timeout=timedelta(seconds=float(os.environ.get("SIGNALBOX_INACTIVITY_TIMEOUT_S", 60))),
    abort_timeout=timedelta(seconds=float(os.environ.get("SIGNALBOX_ABORT_TIMEOUT_S", 45 * 60))),
)


def completed(payload: dict, **extra: Any) -> dict:
    return {**payload, **extra, "terminal": "run.completed"}


def halted(payload: dict, reason: str, **extra: Any) -> dict:
    return {**payload, **extra, "terminal": "run.halted", "reason": reason}


async def step(ctx: WorkflowContext, name: str, act: Callable[[dict], dict], payload: dict) -> dict:
    """One journaled act. A raised exception is retried; a returned dict is data."""

    async def action() -> dict:
        # The acts are blocking subprocess calls; keep them off the event loop
        # so one long review does not stall every other invocation.
        return await asyncio.to_thread(act, payload)

    return await ctx.run_typed(name, action, RunOptions(max_attempts=3))


async def wait_for(ctx: WorkflowContext, name: str, timeout: timedelta) -> dict | None:
    """A durable wait with a durable deadline. None means the deadline won.

    This is the reaper's job, done by the run that is waiting rather than by a
    sweep over marker files.
    """
    match await restate.select(value=ctx.promise(name).value(), deadline=ctx.sleep(timeout)):
        case ["value", value]:
            return value
        case _:
            return None


def set_status(ctx: WorkflowContext, **status: Any) -> None:
    ctx.set("status", status)


@ci_cycle.main()
async def run(ctx: WorkflowContext, opened: dict) -> dict:
    try:
        terminal = await cycle(ctx, dict(opened))
    except TerminalError as exc:
        # An act that raised on every attempt. Without this the invocation would
        # fail with no announce: the one way left to end without a terminal.
        terminal = halted(dict(opened), f"an act failed after its retries: {exc}",
                          stranded_topic="act.failed")
    set_status(ctx, phase="terminal", terminal=terminal["terminal"], reason=terminal.get("reason"))
    # The terminal still reaches Emergent, so clear-pending, notify and the
    # dashboard keep working unchanged while the two systems coexist.
    await step(ctx, "announce", ACTS.announce, terminal)
    return terminal


async def cycle(ctx: WorkflowContext, payload: dict) -> dict:
    ci_round = int(payload.get("ci_round") or 0)
    while True:
        sha = str(payload["sha"])
        set_status(ctx, phase="awaiting-checks", ci_round=ci_round, sha=sha)
        checks = await wait_for(ctx, f"checks:{sha}", CHECKS_TIMEOUT)
        if checks is None:
            return halted(payload, f"no check suite concluded for {sha} within {CHECKS_TIMEOUT}",
                          stranded_topic="checks.silent")
        if checks.get("conclusion") == "success":
            merged = await step(ctx, "merge_pr", ACTS.merge_pr, payload)
            if not merged.get("ok"):
                return halted(merged, merged.get("error") or "merge refused")
            return completed(merged)

        detailed = await step(ctx, f"check_details:{ci_round}", ACTS.check_details,
                              {**payload, **checks, "ci_round": ci_round})
        findings = await step(ctx, f"map_ci_findings:{ci_round}", ACTS.map_ci_findings, detailed)
        if not findings.get("ok"):
            return halted(findings, findings.get("reason") or "CI findings refused",
                          stranded_topic="ci.findings-attempted")
        # map_ci_findings advances the cycle counter; the guard reads the
        # advanced value, exactly as guard-ci-rounds-continue/-exhaust do.
        ci_round = int(findings["ci_round"])
        if ci_round >= MAX_CI_ROUNDS:
            names = ", ".join(str(f.get("name", f)) if isinstance(f, dict) else str(f)
                              for f in findings.get("failed") or [])
            return halted(findings, f"post-merge CI fix rounds exhausted for failed checks: {names}",
                          stranded_topic="ci.fix-ready")

        fixed = await fix_until_approved(ctx, findings, ci_round)
        if fixed.get("terminal"):
            return fixed

        commit = await step(ctx, f"commit_fix:{ci_round}", ACTS.commit_fix, fixed)
        if not commit.get("ok"):
            return halted(commit, commit.get("error") or "CI fix commit failed")

        rebased = await step(ctx, f"rebase:{ci_round}", ACTS.rebase, commit)
        if rebased.get("ok") is not True:
            # The topology sends this to remediation; the spike stops here.
            return halted(rebased, "branch rebase did not apply cleanly",
                          stranded_topic="branch.rebase-conflicted")

        suite = await step(ctx, f"run_suite:{ci_round}", ACTS.run_suite, rebased)
        if suite.get("errored"):
            return halted(suite, "the detected suite could not be invoked")

        gate = await step(ctx, f"assess:{ci_round}", ACTS.assess, suite)
        match gate.get("decision"):
            case "clear":
                pass
            case "needs_human":
                set_status(ctx, phase="awaiting-approval", ci_round=ci_round)
                # No deadline: an approval wait is held for as long as it takes,
                # which is what mark-approval-pending exists to approximate.
                await ctx.promise(f"approval:{ci_round}").value()
            case "block":
                return halted(gate, gate.get("reason") or "promotion gate blocked",
                              stranded_topic="gate.blocked")
            case other:
                return halted(gate, f"promotion gate returned an invalid decision: {other!r}",
                              stranded_topic="gate.invalid-verdict")

        pushed = await step(ctx, f"push_branch:{ci_round}", ACTS.push_branch, gate)
        if not pushed.get("ok"):
            return halted(pushed, pushed.get("error") or "push failed",
                          stranded_topic="branch.push-failed")
        payload = {**payload, "sha": pushed["sha"], "ci_round": ci_round}


async def fix_until_approved(ctx: WorkflowContext, findings: dict, ci_round: int) -> dict:
    """The inner review loop. Returns the approved fix, or a terminal."""
    attempt = dict(findings)
    for review_round in range(1, MAX_REVIEW_ROUNDS + 1):
        attempt["round"] = review_round
        set_status(ctx, phase="fixing", ci_round=ci_round, round=review_round)
        await step(ctx, f"start_fix:{ci_round}:{review_round}", ACTS.start_fix, attempt)
        submitted = await wait_for(ctx, f"fix:{ci_round}:{review_round}", FIX_TIMEOUT)
        if submitted is None:
            return halted(attempt, f"fixer was silent for {FIX_TIMEOUT}", stranded_topic="shard.silent")
        if submitted.get("verdict") != "done":
            return halted(attempt, f"fixer submitted {submitted.get('verdict')!r}",
                          stranded_topic="shard.abandoned")

        review = await step(ctx, f"review:{ci_round}:{review_round}", ACTS.review,
                            {**attempt, **submitted})
        match review.get("verdict"):
            case "approved":
                return {**attempt, **submitted}
            case "changes_requested":
                attempt = {**attempt, "findings": review.get("findings") or []}
            case other:
                return halted(attempt, f"post-merge CI fix review returned an invalid verdict: {other!r}",
                              stranded_topic="review.invalid-verdict")
    return halted(attempt, "post-merge CI fix review never approved after five rounds",
                  stranded_topic="ci.changes-requested")


# ── resolvers: the edges that used to be topics ─────────────────────────────


async def resolve(ctx: WorkflowSharedContext, name: str, value: dict) -> dict:
    try:
        await ctx.promise(name).resolve(value)
    except TerminalError as exc:
        # A redelivered webhook or a re-announced verdict. The first one won and
        # the run already acted on it; the second has nothing to change.
        return {"resolved": False, "promise": name, "reason": str(exc)}
    return {"resolved": True, "promise": name}


@ci_cycle.handler()
async def checks_concluded(ctx: WorkflowSharedContext, suite: dict) -> dict:
    """check_suite.completed for this run's PR, as route-check-suite projects it."""
    conclusion = suite.get("conclusion")
    if conclusion not in ("success", "failure"):
        # Stale and cancelled suites are what a force-push leaves behind; the
        # topology's check router drops them for the same reason.
        return {"resolved": False, "reason": f"ignored conclusion {conclusion!r}"}
    return await resolve(ctx, f"checks:{suite['sha']}", suite)


@ci_cycle.handler()
async def fix_submitted(ctx: WorkflowSharedContext, submission: dict) -> dict:
    """The fixer's shard.submitted announcement, forwarded by the control endpoint."""
    return await resolve(ctx, f"fix:{submission['ci_round']}:{submission['round']}", submission)


@ci_cycle.handler()
async def approve(ctx: WorkflowSharedContext, grant: dict) -> dict:
    status = await ctx.get("status") or {}
    ci_round = grant.get("ci_round", status.get("ci_round"))
    return await resolve(ctx, f"approval:{ci_round}", grant)


@ci_cycle.handler()
async def status(ctx: WorkflowSharedContext) -> dict:
    return await ctx.get("status") or {}
