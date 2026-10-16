# Message Latency Implementation Plan — P0 Timing

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for inline execution, or superpowers:subagent-driven-development if the owner selects delegation. Steps use checkbox syntax for tracking.

**Goal:** Identify latency at existing dispatch boundaries without changing execution order or retry behavior.

**Architecture:** Share one bounded, thread-safe timing collector between dispatch and Telegram. Measure durations with an injected monotonic clock; read persisted queue timestamps with an injected UTC clock and label their age separately. Introduce no execution workers in this increment.

**Tech Stack:** Python standard library, unittest, SQLite fixtures, existing controller.

**Spec:** `docs/MESSAGE_LATENCY_DESIGN.md`, first increment. Separate plans will cover durable delivery, worker scheduling and Gmail, since each changes independent contracts. This increment does not complete all of `plan.md`.

## Global Constraints

- Preserve existing working-tree changes, durable messages, memory, conversation IDs and spending records.
- Do not automatically replay uncertain inputs or deliveries.
- Use isolated fixtures for verification; do not wake Andrew or make paid requests for tests.
- Do not collect bodies, credentials, tool arguments or raw exception messages.
- Never subtract monotonic timestamps from different processes.
- No production installation, service restart or manifest renewal in this increment.

## Review Focus

- Clock adjustments: UTC queue age must be clamped at zero and labeled as wall-clock age.
- Restart: reconstruct persisted queue age without reusing an old monotonic clock value.
- Concurrent and nested operations: snapshots must not lose active measurements or deadlock.
- Remote exceptions: diagnostics must never retain exception text, payloads or credentials.
- Empty or failed stages: record completion/failure without masking the original error.

## Task 1: Bounded timing collector

**Files:** Create `prototype/andrewbot/latency.py` and `prototype/tests/test_latency.py`.

**Interfaces:** `LatencyMetrics(clock=time.monotonic)`, `measure(stage)` as a context manager, `observe(stage, seconds)`, and `snapshot() -> dict`. Accept only a fixed stage allowlist: `poll`, `inbox_wait`, `control`, `chat`, `schedule`, `queued`, `autonomous`, `outbox_wait`, `send`, `diary`, `backup`. Snapshot per-stage count, failures, fixed error-category counts, total/last/max seconds, last-success monotonic timestamp, active count and oldest active age. Keep aggregate state per stage and active measurement tokens only; no duration sample history. Remove active tokens in `finally`.

- [x] Write tests using an injected fake clock: a 2-second successful measure yields count 1, failures 0, total/last/max 2; a failing measure increments failures and re-raises the same exception object.
- [x] Add tests for overlapping/nested stages, concurrent snapshots, invalid stage rejection, constant completed-history storage and no exception text in serialized snapshots.
- [x] Run `PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -p test_latency.py -v`; confirm failure because the collector does not exist.
- [x] Implement the interfaces with a reentrant lock held only during state access. Do not hold it around measured work.
- [x] Repeat the command; require all tests to pass. Review the diff and commit only this task's files if commits are requested; never stage unrelated changes.

## Task 2: Dispatch timing and persisted queue age

**Files:** Modify `prototype/andrewbot/dispatch.py`, `prototype/andrewbot/store.py`, `prototype/tests/test_dispatch.py`; extend `prototype/tests/test_latency.py` for age parsing.

**Interfaces:** Add optional `metrics=None` to `ControllerDispatch.__init__`; expose `dispatch.metrics`. Add read-only Store methods `inbox_received_at(update_id) -> str | None` and `reply_age_seconds(reply_id, now) -> float | None`. The second uses an additive nullable UTC `created_at` outbox column; existing rows remain unknown, never backfilled with invented receipt times. New outbox rows receive actual UTC enqueue time. Keep all existing method signatures compatible.

- [x] Add failing tests that block a chat fixture while taking a metrics snapshot: `chat` is active, a separately executed control completes, and no model call is added by observation.
- [x] Add tests for persisted inbox age after reopening the fixture database; assert future timestamps clamp to zero and invalid/unknown timestamps omit the observation.
- [x] Add migration tests with a legacy outbox schema: existing content, IDs, attempts and delivery progress survive, and unknown legacy age is returned as `None`. Verify new replies have a timestamp and increasing age after reopen.
- [x] Run `PYTHONPATH=prototype:prototype/tests python3 -m unittest test_dispatch test_latency -v`; confirm the new assertions fail before implementation.
- [x] Instrument `_poll_loop`, synchronous polling, `_handle_claimed_update`, each schedule check, `flush_queued`, `sessions.tick`, and `_deliver_reply`. Measure control/chat execution independently of waiting. Observe inbox age immediately before claimed handling and outbox age immediately before delivery. Preserve claims, locks, stop checks and ordering.
- [x] Run the same tests; require all to pass. Inspect that timing records contain fixed stage names only and no message IDs or content. Review the diff before any task-only commit.

## Task 3: Telegram and main-loop boundaries

**Files:** Modify `prototype/andrewbot/telegram.py`, `prototype/andrewbot/main.py`, `prototype/tests/test_telegram.py`; add `prototype/tests/test_latency_integration.py`. Review correction additionally instruments existing caught-error handlers in `controller.py`, `sessions.py` and `diary.py`; their return values and policies stay unchanged.

**Interfaces:** Add optional `metrics=None` to `TelegramAPI.__init__`. Measure individual `sendMessage` API calls as `send`; retain current timeout values and call signatures. Pass the same collector from `main.py` to Telegram and dispatch; measure actual backup and diary work at their existing call sites. Do not add a diagnostics timer that touches the network or storage.

- [x] Write fake-opener tests for successful send, timeout and malformed response; require one `send` observation per API call and the original credential-redacted public exception behavior.
- [x] Test that `getUpdates` is counted only at the dispatch polling boundary and not as interactive sending; multipart sends produce separate observations.
- [x] Add a fixture integration test proving slow send duration and chat handling duration appear separately, including exceptional exit; assert no changed retry, delivery, model or spending behavior.
- [x] Run `PYTHONPATH=prototype:prototype/tests python3 -m unittest test_telegram test_latency_integration -v`; confirm the new tests fail before wiring metrics.
- [x] Wire the collector and context managers; repeat the tests until passing. No new network timeout settings in this increment.

## Task 4: Regression verification and handoff

**Files:** Update `plan.md` with completed timing work and remaining items; update this plan's checkboxes with actual results. Leave the incident fix manifest unchanged: runtime sources will need separate review before installation.

- [x] Run `PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -q`; require no failures and report optional skips.
- [x] Run `ANDREW_LETTA_TEST_BINARY=/var/lib/andrew/letta-app/node_modules/@letta-ai/letta-code/letta.js python3 -m unittest discover -s scripts/tests -q` when the pinned fixture exists; use only fake providers, never production input.
- [x] Run `git diff --check`; inspect the final diff for secret-bearing logging, data mutation beyond the additive timestamp migration, changed replay behavior and shutdown ordering.
- [x] Report completed source work, test counts and limitations. No production latency claim without a production observation. Prepare the next design-backed plan for held delivery states and independent durable senders before changing delivery behavior.

## Execution decision

Approved: inline implementation by the current agent, because this first increment shares a small collector interface across existing files and does not benefit from separate agent contexts. The owner approved this plan and inline execution. Existing uncommitted work stays in place; do not reset, stash, move or commit it wholesale.


## Verification results

- Core: 267 tests run, 265 passed, 2 optional skips (12.452 seconds).
- Scripts: 53 test methods run; 52 passed. The deployment guard
  `test_checked_in_deployment_manifest_matches_prepared_sources` failed in seven
  subtests: `controller.py`, `diary.py`, `dispatch.py`, `main.py`, `sessions.py`,
  `store.py`, `telegram.py` (53 test methods in 31.432 seconds). The incident
  manifest still describes the previously reviewed installation; new source
  hashes and the added `latency.py` dependency require a separate deployment
  review. The guard was not weakened and no hashes were renewed.
- Focused RED→GREEN runs: collector 6 tests; dispatch/collector/age 29 tests;
  Telegram/integration 6 tests. After the review correction, the combined focused
  set passed 40 tests. Model and network delays use isolated fake clocks.
- Production files, databases, services, messages and spending were not touched.
- Next increment prepared in `docs/MESSAGE_DELIVERY_IMPLEMENTATION.md`.


## Review correction

Independent review found that model/session/diary methods swallow operational
exceptions, so outer timing alone incorrectly marked failures successful. The
collector now provides `record_failure(error, category=None)` for existing catch
handlers. It marks the current context-local span without changing the operation's
return value and does nothing outside a measurement. Only fixed error categories
are retained; exception objects, messages and arbitrary class names are discarded.
Idle and budget-gated returns remain healthy; cancellation has a fixed label.

Real-component fixtures reproduced the failed chat, queued model, autonomous
model, diary send and post-cycle ledger paths before the correction, then passed.
No new replay or delivery policy was introduced. The planned files were expanded
only to mark those existing internal catches, as required by the approved design.


Final review: both Important findings were fixed in one RED→GREEN pass and the
full core suite rerun. No Critical or Minor findings were reported. Deployment
is not ready: the preserved manifest guard fails as documented. No merge, commit,
installation, or production service operation was performed.
