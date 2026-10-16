# Message worker, interactive timeouts and health implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Preserve the previously chosen inline execution method.

**Goal:** Keep the scheduler responsive during model and diary network work and expose local queue and worker health.

**Architecture:** One model worker owns normal input handling, queued-message turns and autonomous turns. Its wake event carries no message bodies or input claims: SQLite remains the durable source of work. A separate diary sender owns diary network delivery. Existing poll/control and two reply sender threads retain their responsibilities.

**Tech Stack:** Python standard library threads/events, SQLite Store, unittest and existing latency metrics.

**Spec:** `docs/MESSAGE_LATENCY_DESIGN.md`, third increment; progress tracked in `plan.md`.

## Global constraints

- Preserve existing working-tree changes, durable messages, memory, conversation IDs and spending records.
- Do not automatically replay uncertain inputs or deliveries.
- Do not introduce concurrent Letta turns.
- Do not wake Andrew or make paid requests for tests.
- Interactive socket timeout starts at five seconds; polling timeout must exceed the requested long-poll duration. These are socket timeouts, not total deadlines.
- Preserve the installed 6000 output-token limit and existing model cancellation/deadline policies.
- Source implementation does not deploy, restart services, replay messages or resume automatic model calls.

## Review focus

1. Sleep/pause/wake commands arriving during a turn must retain their authoritative lifecycle state.
2. A crash after input claim must retain uncertain input, while merely waking an idle worker must claim nothing.
3. Ordinary input arriving before autonomy admission must take priority without starving local schedule checks.
4. Explicit diary retry racing its sender must never produce overlapping sends.
5. Shutdown during blocked network work must keep shared storage open until every user of it exits.

## File responsibilities

- Create `prototype/andrewbot/workers.py`: event-driven worker lifecycle, bounded wake notifications and sanitized snapshots; no persistent work payloads.
- Modify `dispatch.py`: start/stop ownership and move its existing model-bearing sequence into the single worker.
- Modify `main.py`: schedule diary work asynchronously and include all workers in resource shutdown.
- Modify `store.py`: local health aggregates and exclusive diary delivery ownership where existing chunk authorization alone is insufficient.
- Modify `controller.py`: owner-only `/health` using injected local snapshot callable.
- Modify `telegram.py`: separate interactive and long-poll socket timeout selection.
- Modify `telegram_menu.py` only if needed to register `/health` consistently with existing commands.
- Tests belong in new `test_model_worker.py`, `test_health.py`, and existing dispatch, diary, Telegram and shutdown suites.

### Task 1: Serialize model work outside the scheduler

**Interfaces:** `ModelWorker(action, should_stop)` in `workers.py` provides `start()`, `wake()`, `stop(timeout=2) -> bool`, and `snapshot() -> dict`. The action is one bounded scheduling pass, not one unbounded drain. Snapshot has fixed state (`idle`, `running`, `stopping`, `stopped`), monotonic active age and last completion time; no exception text or payloads.

- [ ] Write blocked-fixture tests: repeated wake calls during one action do not overlap actions; notifications coalesce; an exception is recorded and does not silently kill the thread; stop prevents another action.
- [ ] Run `PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -p test_model_worker.py -v`; observe expected missing-worker failures.
- [ ] Implement worker lifecycle using a single event, one thread and a snapshot lock; no volatile job queue.
- [ ] Add integration tests: blocked `handle_update()` leaves `dispatch.step()` and schedule checks responsive; `/status` still completes through the system lane; normal, queued and autonomous fake model calls have maximum concurrency one.
- [ ] Move `process_updates()`, `flush_queued()` and `sessions.tick()` from `step()` into one worker pass in that order. Keep controls on the poll thread. Check stop between stages. Recheck durable pending owner input before autonomy using the existing session admission checks. Preserve rate limiting and stage metrics. Do not claim an inbox row when merely notifying the worker.
- [ ] Add deterministic tests for pending owner input before autonomy, sleep/pause cancellation and authoritative owner revision, queued input surviving restart, and claimed interrupted input remaining held. `/replay` remains explicit local requeue authorization; do not invoke it in tests against production.
- [ ] Run the worker and existing dispatch/session tests, then the full core suite. Commit only the worker increment after green results.

### Task 2: Move diary delivery off the scheduler and close safely

**Interfaces:** Reuse Task 1 worker lifecycle for a dedicated diary action; dispatch owns or explicitly receives both worker lifecycles. `ControllerDispatch.stop(timeout=2) -> bool` reports all poll, reply, model and diary threads stopped, with a shared monotonic shutdown deadline. `close_controller_resources()` consumes that result before closing shared storage.

- [ ] Add tests proving blocked diary delivery does not block `step()` or system replies, concurrent explicit retry cannot overlap an active diary send, acknowledged parts stay acknowledged, and unacknowledged parts remain held after restart.
- [ ] Observe RED with the current synchronous main-loop diary calls.
- [ ] Replace synchronous diary delivery calls in `main.py` with wake notifications. Keep one diary sender; preserve existing explicit owner retry semantics and atomic chunk authorization. If retry can reset authorization during an active send, add transactional diary claim ownership checked by both retry and sender, and recover interrupted ownership conservatively at startup.
- [ ] Extend shutdown tests to block model and diary actions independently, verify stop forbids new admissions, and verify history/database remain open on incomplete shutdown. Retain existing cancellation request on SIGTERM.
- [ ] Run diary, shutdown, delivery and full core suites; commit after green results.

### Task 3: Bound interactive Telegram socket waits

**Interfaces:** Preserve `TelegramAPI(..., timeout=...)` as an explicit compatibility override. With no override, interactive methods use 5 seconds; `getUpdates` uses requested poll duration plus 5 seconds (default 25 -> 30, background 1 -> 6). Reject invalid poll durations before opening a socket.

- [ ] Add fake-opener tests asserting exact timeouts for sendMessage/getMe, 25-second and 1-second polls, and explicit timeout overrides. Observe RED on current shared 35-second default.
- [ ] Implement method-specific timeout selection in `_call()` without automatic retries. Preserve token-redacted errors and timing metrics.
- [ ] Test timeout after persisted send attempt: no second send occurs and the delivery becomes held. Test HTTP 429 does not cause immediate repeated send attempts.
- [ ] Run Telegram and delivery suites plus full core suite; commit after green results.

### Task 4: Add owner-only local health diagnostics

**Interfaces:** Inject a `health_snapshot()` callable into Controller (optional for existing constructors). Dispatch supplies worker snapshots plus metrics; Store supplies read-only queue aggregates. `/health` renders bounded plain text with queued/held inputs, pending/held replies per lane, oldest pending age, model/diary state and active age, stage durations and most recent successful completion. Empty data is reported as unavailable rather than a fabricated zero-age success.

- [ ] Add tests for unauthorized requests, paused/error state, empty queues, legacy persisted timestamps and a blocked model/sender. Assert no model or network call, no message text, credentials or raw exception text, and system-lane response delivery.
- [ ] Observe RED because `/health` is not implemented.
- [ ] Add read-only aggregate Store queries, assemble snapshots without holding database locks across network/model work, and render `/health` inside the existing authenticated command route. Avoid exposing claim tokens or identifiers unnecessary for diagnosis.
- [ ] Run health, controls, latency and full core suites; commit after green results.

### Task 5: Review and installation preparation boundary

- [ ] Run `PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -q`; all mandatory tests must pass, optional skips documented.
- [ ] Run script tests with the existing Letta test binary. New/changed source hashes may require separate reviewed deployment-manifest preparation; record expected manifest mismatches precisely, never describe that suite as passing if it fails.
- [ ] Obtain one fresh-context whole-change review under executing-plans; fix important findings with reproducing tests, rerun affected and full core suites.
- [ ] Update `plan.md` and the design status with source-only completion and actual test results. Record remaining production preparation, including manifest coverage for `workers.py`, restored-file behavior and fail-closed rollback after attempted startup.
- [ ] Commit/push/deploy only under the applicable existing user authorization; do not resume Andrew or replay held messages as part of verification.

## Self-review

The worker, timeout and health tasks cover every requirement of the approved third increment. Diary concurrency and shutdown are explicit work rather than incidental cleanup. Existing Store and session admission/revision checks remain authoritative; tests must pin their behavior before moving execution threads. Production diagnostics remain read-only, and the source plan introduces no new paid requests or replay authority.

Status: prepared for owner review; implementation has not started.
