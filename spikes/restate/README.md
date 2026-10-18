# Spike: the post-merge CI cycle on Restate

**Question:** should a run's lifecycle be durable code instead of event
choreography? The stretch tested is the post-merge CI cycle: the part of
`emergent.toml` behind #145 and #151 (stranded runs), #139 (restarts that strand
a live run) and the orchestrator's babysitter (#161).

**Answer so far:** yes for this stretch. The strand classes those incidents fixed
one at a time cannot happen here, and every crash point tested resumes on its
own. What remains open is the cost of running it in production (below), and
whether Emergent should keep owning this part of signalbox.

## What is here

| File | What it is |
|---|---|
| `ci_cycle/workflow.py` | The cycle as one Restate workflow, keyed by `run_id` (about 250 lines including docstrings) |
| `ci_cycle/acts.py` | `RealActs`, a thin adapter over the existing `signalbox.acts` functions and the `signalbox agent` / `signalbox dispatch` CLIs; and `FakeActs`, scripted stand-ins that log every execution to a ledger file |
| `serve.py` | Serves the endpoint in a single process, so a SIGKILL is a real crash |
| `tests/` | Runs a real `restate-server` binary and the service as separate processes, and kills them |

Nothing under `src/` changes. The topology still runs this path today.

## Mapping from the topology

| Topology | Workflow |
|---|---|
| About 42 handlers and sinks: `route-checks-*`, `detail-failed-checks`, `map-ci-to-findings`, `guard-ci-rounds-*`, `route-ci-*`, `guard-ci-review-rounds-*`, `commit-ci-fix`, `rebase-branch`, `run-suite`, `assess`, the `route-gate-*` routers, `push-branch`, `merge-pr` and their `-failed`/`-invalid` arms | `cycle()` and `fix_until_approved()`: ordinary `if`/`match`/`for` |
| `mark-pr-pending`, `rehydrate-pr`, `reap-prs` → `checks.silent` | Durable promise `checks:<sha>`, raced against a durable timer |
| Reaper → `shard.silent` | Durable promise `fix:<ci_round>:<round>`, raced against a durable timer |
| `mark-approval-pending`, `clear-approval-pending`, `signalbox approve` | Durable promise `approval:<ci_round>` (no deadline) |
| Babysitter relaunch (orchestrator #161), restart remediation (#139, #153) | Restate retries the invocation from its journal |
| The `stranded_topic` field on every halt | Still set on every `halted()`, so halts read the same |

Every path out of `cycle()` is a `return` of `completed(...)` or `halted(...)`,
and `run()` turns an act that exhausts its retries into `halted(...)` too.
There is no topic without a consumer, and no join that can fire too early. Those
were the failures in #128, #123, #151 and #145.

## What the tests prove

Run them with:

```bash
uv sync --group restate-spike
npm i @restatedev/restate-server@1.8.0   # or any restate-server binary
export RESTATE_SERVER_BIN=$PWD/node_modules/@restatedev/restate-server-linux-x64/bin/restate-server
cd spikes/restate && uv run pytest
```

All 12 pass, in about 25 s, on repeated runs.

| Test | What it shows |
|---|---|
| `red_then_green_runs_every_act_once` | One red cycle end to end; the ledger shows every act exactly once |
| `reviewer_rejection_loops_inside_the_cycle` | A reviewer's rejection re-runs the fixer within the same CI cycle (#145) |
| `service_killed_mid_step_resumes_…` | Kills the service inside the review, after its side effect but before it is saved. After a plain restart, with no re-registration and no babysitter, only that step runs again; nothing already finished is repeated |
| `whole_host_restart_while_waiting_on_ci` | SIGKILLs both the server and the service while the run is parked on CI; it resumes when the webhook arrives |
| `webhook_arriving_before_the_wait_is_not_lost` | A check_suite result that lands before the workflow is waiting is still consumed. The topology needs `mark-pr-pending` plus `rehydrate` for this |
| `redelivered_and_stale_webhooks_change_nothing` | Cancelled suites, redeliveries and results for a superseded sha are all no-ops |
| `human_gate_holds_across_a_restart` | The approval wait survives a service restart; push happens only after `approve` |
| `reviewer_that_never_approves_halts_with_a_reason` | Inner budget of 5 rounds, then `run.halted` with `stranded_topic: ci.changes-requested` |
| `ci_that_stays_red_halts_after_the_cycle_budget` | Outer budget matching `guard-ci-rounds-*`, halting with the failed check names |
| `silent_fixer_times_out_instead_of_hanging` | A fixer that never announces becomes `shard.silent` via a timer inside the run, with no reaper |
| `an_act_that_keeps_raising_still_ends_in_a_terminal` | An act that fails on every attempt (3) still ends in an announced `run.halted` (`stranded_topic: act.failed`), which is what the `-e` failure topics were missing in #110/#123 |
| `a_step_longer_than_the_abort_timeout_is_replayed` | **The trap, measured:** a step that outlives the abort timeout is cancelled and runs again |

## What it does not prove

- **`RealActs` has not run against real git, gh or models.** It calls the same
  functions and CLIs the exec-handlers call, but no end-to-end run went through
  it.
- **Three forwarders would be needed to wire it in.** Each is a small Emergent
  sink that POSTs to Restate ingress:
  1. `checks.reported` → `CiCycle/<run_id>/checks_concluded`
  2. `shard.submitted` where `ci_origin == "post-merge"` → `fix_submitted`
  3. `approval.granted` → `approve`

  Plus one entry edge, `pr.opened` → `CiCycle/<run_id>/run/send`.
- **Only this stretch.** Planning, stages, shards and the stage join are
  untouched. The shard fan-out would be `restate.gather` over shard promises,
  but that hasn't been tried.
- **Remediation is out of scope.** A rebase conflict halts instead of entering
  the remediation loop.

## Costs and traps found

1. **Abort timeout.** Restate cancels an invocation that makes no journal
   progress for `inactivity_timeout` + `abort_timeout` (server defaults: 1 m +
   10 m), and the in-flight step runs again. A 15-minute review would be
   dispatched twice under the defaults. The workflow sets 45 minutes, and the
   fixer is not a step at all: it is spawned, and the run suspends on its
   promise. Any act that can exceed the timeout must be built that way or be
   idempotent.
2. **Steps run at least once.** A crash between a step's side effect and its
   journal entry repeats the step (the mid-review kill test shows exactly this).
   `RealActs.start_fix` uses an `O_EXCL` claim so a fixer is never dispatched
   twice. `commit_fix`, `push_branch` and `merge_pr` are already close to
   idempotent (`--force-with-lease`, state checks), but each needs auditing.
3. **Deployment versioning.** As I understand Restate's model, an in-flight
   invocation stays pinned to the deployment it started on. Changing
   `workflow.py` therefore means registering a new deployment and keeping the
   old one serving until its runs drain. That is new operational work; Emergent
   handlers have no equivalent.
4. **Determinism.** Workflow code replays from the journal, so anything
   non-deterministic (time, randomness, reading files) must go through
   `ctx.run`, `ctx.time()` or `ctx.random()`. The acts already sit behind
   `ctx.run`; the rule needs to be stated for anyone editing the workflow.
5. **Visibility.** Intermediate topics no longer exist, so the dashboard and
   `trace` would see only the entry event and the terminal `announce`. Restate
   has its own UI and SQL introspection over journals, but that is a second
   place to look. Emitting progress events from the workflow is an easy fix if
   wanted.
6. **Operating it.** It is one more stateful service (a single binary with
   embedded RocksDB). Things noticed while running it:
   - `disable-telemetry` defaults to **false**; set `RESTATE_DISABLE_TELEMETRY=true`.
   - It binds `[::]` by default and fails on IPv4-only hosts; use `--bind-ip 127.0.0.1`.
   - It must reach the service without the outbound proxy (`NO_PROXY`).
   - The server is BSL 1.1 (free to self-host, not to resell as a hosted
     Restate platform); the SDK is MIT.
7. **The `hypercorn` CLI spawns a worker child that survives a SIGKILL to its
   parent.** That is a supervision hazard in production too. `serve.py` serves
   in-process instead.

## Decision this informs

The spike shows the mechanics work and remove the strand classes. Whether to go
further is mostly about ownership, not technology: moving run lifecycles into
Restate takes control flow out of the Emergent topology. A reasonable next step,
if wanted, is to run `RealActs` against the demo crate (`demo/`) for one real
red-then-green cycle behind the three forwarders, with the topology's CI path
switched off for that run only.
