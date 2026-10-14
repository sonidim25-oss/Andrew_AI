# Phase 2 Lifecycle and Autonomous Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement durable lifecycle controls, restart recovery, autonomous session/cycle limits, and rollover-aware budget enforcement for Andrew.

**Architecture:** Keep `Controller` and `ModelGateway` as trusted policy boundaries and SQLite as the durable source of lifecycle, audit, cycle, and usage state. Add a controller-owned session runner; preserve the existing Letta, Telegram, and model gateway interfaces except for lifecycle callbacks and budget snapshots.

**Tech Stack:** Python 3.12, SQLite, `zoneinfo`, systemd-managed services, Letta App Server client, existing `unittest` suite.

**Spec:** `docs/superpowers/specs/2026-10-14-phase2-lifecycle-design.md`

**Implementation status (2026-10-14):** Phase 2 code and documentation are implemented in the prototype. Autonomous activation is source-gated until all project phases are complete and reviewed. The source passed `py_compile`; the unit suite was not run in this implementation pass.

## Global Constraints

- Use `America/Toronto` for daily dates and the 09:00 fallback wake; persist absolute wake deadlines in UTC.
- Enforce a $10.00 calendar-month hard cap and protect a $1.00 reserve for bounded wrap-up only.
- Accrue $0.30 daily allowance and roll unused allowance forward only within the calendar month.
- Limit autonomous work to 150 cycles per Toronto calendar day and 180 minutes per session.
- Andrew selects optional work/sleep timing through lifecycle tools; keep only the owner-confirmed 09:00 `America/Toronto` fallback as a fixed schedule point.
- Do not start a live autonomous session or enable unattended scheduling during Phase 2; defer both until all project phases are complete.
- Keep shell, browser, social, email, and other worker capabilities disabled.
- Never expose credentials, private reasoning, or raw provider error details in logs, events, or user-facing status.

## Review Focus

- Toronto DST gaps/folds and host timezone differences: pin with `test_fallback_wake_uses_toronto_date_across_dst` in Task 2.
- Wake polling and restart races causing duplicate state transitions: pin with `test_due_wake_is_recovered_and_audited_once_after_restart` in Task 2.
- Duplicate or uncertain provider work corrupting cycle and cost accounting: pin with `test_interrupted_cycle_is_not_replayed_and_reserved_cost_is_retained` in Task 5.
- Month boundary, rollover, and reserve interaction allowing overspend: pin with `test_rollover_never_spends_protected_reserve_or_crosses_month_cap` in Task 3.
- Pause/sleep transitions during a session allowing another cycle: pin with `test_session_stops_before_next_cycle_after_pause_or_sleep` in Task 4.

---

### Task 1: Durable lifecycle, audit, cycle, and allowance storage

**Files:**
- Modify: `prototype/andrewbot/store.py`
- Test: `prototype/tests/test_store.py` (create)

**Interfaces:**
- Produces `Store.transition_state(updates: dict[str, str], event: str) -> None`, which applies all key updates and one audit event in a single transaction.
- Produces `Store.begin_cycle(session_id: str, cycle_id: str, local_day: str, reserved_cost_usd: float = 0.0) -> None`, `Store.finish_cycle(cycle_id: str, status: str, actual_cost_usd: float | None = None) -> bool`, and `Store.cycle_count(local_day: str) -> int`.
- Produces `Store.process_fallback_wake(local_day: str, owner_id: int) -> bool`, atomically claiming that Toronto day and waking only indefinite sleep.
- Produces `Store.usage_totals(month: str, day: str) -> dict` with existing keys preserved.

- [ ] **Step 1: Write failing storage tests** `test_transition_state_commits_state_and_audit_together`, `test_fallback_date_claim_is_idempotent`, `test_cycle_count_uses_toronto_day_key`, and `test_existing_database_schema_migrates_without_losing_rows`; assert the persisted state, event count, claim return values, cycle count, and retained messages/usage.
- [ ] **Step 2: Run `python3 -m unittest prototype.tests.test_store -v`** and confirm the new tests fail on missing methods/schema.
- [ ] **Step 3: Implement the transaction and schema changes** with SQLite transactions and an indexed cycle ledger; preserve all existing rows and APIs.
- [ ] **Step 4: Run `python3 -m unittest prototype.tests.test_store -v`** and confirm all storage tests pass.

### Task 2: Lifecycle transitions, wake scheduling, and restart recovery

**Files:**
- Modify: `prototype/andrewbot/controller.py`
- Modify: `prototype/andrewbot/main.py`
- Modify: `prototype/tests/test_controller.py`

**Interfaces:**
- Consumes Task 1 `transition_state(...)` and `process_fallback_wake(...)`.
- Produces `Controller.recover_lifecycle(now: datetime | None = None) -> list[str]`, which resolves expired persisted wakes and returns safe event summaries.
- Retains `check_scheduled_wake(now=None) -> bool` and `check_fallback_wake(now=None) -> bool` signatures.

- [ ] **Step 1: Add failing controller tests** `test_lifecycle_commands_write_state_and_event_atomically`, `test_fallback_checkpoint_is_consumed_once_while_awake`, `test_due_wake_is_recovered_and_audited_once_after_restart`, and `test_fallback_wake_uses_toronto_date_across_dst`; assert state and event persistence, no later same-day wake after an awake checkpoint, one transition after repeated polling/reopen, and correct local-day selection.
- [ ] **Step 2: Run `python3 -m unittest prototype.tests.test_controller -v`** and confirm the relevant tests fail.
- [ ] **Step 3: Route lifecycle mutations through `Store.transition_state`** and implement `recover_lifecycle`, idempotent wake processing, and Toronto-local fallback-date handling.
- [ ] **Step 4: Invoke recovery before Telegram polling in `main.run`** and process any wake-triggered queue only after lifecycle state is reconciled.
- [ ] **Step 5: Run `python3 -m unittest prototype.tests.test_controller -v`** and confirm lifecycle, timezone, and recovery cases pass.

### Task 3: Calendar-month budget, daily rollover, and reserve enforcement

**Files:**
- Modify: `prototype/andrewbot/store.py`
- Modify: `prototype/andrewbot/controller.py`
- Modify: `prototype/andrewbot/gateway.py`
- Modify: `prototype/andrewbot/main.py`
- Modify: `prototype/tests/test_controller.py`
- Modify: `prototype/tests/test_gateway.py`

**Interfaces:**
- Produces a shared budget calculation `budget_snapshot(now: datetime) -> dict` in `prototype/andrewbot/budget.py`, containing Toronto month/day, month spend, daily spend, accrued rollover allowance, ordinary spend ceiling, reserve, and remaining ordinary allowance.
- `ModelGateway` and `Controller` both consume the same calculation; the gateway remains the final request gate.

- [ ] **Step 1: Add failing budget tests** `test_first_local_day_accrues_thirty_cents`, `test_unused_daily_allowance_rolls_forward_within_month`, `test_new_month_resets_rollover_pool`, `test_rollover_never_spends_protected_reserve_or_crosses_month_cap`, and `test_unknown_provider_usage_consumes_reserved_maximum`; assert ordinary allowance values, retained $1 reserve, and total spend never exceeding $10.
- [ ] **Step 2: Run `python3 -m unittest prototype.tests.test_controller prototype.tests.test_gateway -v`** and confirm new rollover tests fail.
- [ ] **Step 3: Implement `budget_snapshot(now)`** using Toronto calendar boundaries, current-month ledger spend, a $0.30 allowance accrued per elapsed local calendar day, and a $9.00 ordinary spend ceiling; reserve up to $1.00 for bounded wrap-up without crossing $10.00 total.
- [ ] **Step 4: Update controller preflight, gateway reservation/reconciliation, and `/budget` output** to consume the same snapshot and fail closed if it cannot be read.
- [ ] **Step 5: Run `python3 -m unittest prototype.tests.test_controller prototype.tests.test_gateway -v`** and confirm all budget tests pass.

### Task 4: Autonomous session runner and independent cycle ceiling

**Files:**
- Create: `prototype/andrewbot/sessions.py`
- Modify: `prototype/andrewbot/controller.py`
- Modify: `prototype/andrewbot/main.py`
- Modify: `prototype/andrewbot/appserver.py`
- Create: `prototype/tests/test_sessions.py`
- Modify: `prototype/tests/test_appserver.py`

**Interfaces:**
- Produces `SessionRunner.start(trigger: str, now: datetime) -> str | None`, `SessionRunner.run_cycle(session_id: str) -> bool`, and `SessionRunner.stop(reason: str) -> None`. Andrew may continue, sleep for 1–1440 minutes, or request a local wake time after each completed turn; all sessions remain capped at 180 minutes.
- A session cycle is one completed Letta work turn, including controller-mediated tools. Retries and owner Telegram requests are excluded.
- A runner cycle may begin only while awake, unpaused, below 150 Toronto-day autonomous cycles, below the 180-minute session ceiling, and within ordinary budget allowance.

- [ ] **Step 1: Add failing session tests** `test_fallback_wake_starts_session_when_waking_indefinite_sleep`, `test_agent_requested_wake_can_start_an_optional_session`, `test_agent_can_choose_timed_sleep_or_local_wake`, `test_daily_cycle_ceiling_applies_across_multiple_sessions`, `test_session_stops_at_180_minutes`, `test_completed_letta_turn_with_tools_counts_as_one_cycle`, `test_budget_denial_prevents_session_start`, and `test_session_stops_before_next_cycle_after_pause_or_sleep`; assert agent-selected lifecycle transitions, exact cycle count, and no extra Letta call after a stop condition.
- [ ] **Step 2: Run `python3 -m unittest prototype.tests.test_sessions -v`** and confirm the new tests fail.
- [ ] **Step 3: Implement `SessionRunner`** using the Letta agent callback and Task 1/3 storage and budget interfaces; stop before starting another cycle after any sleep, pause, stop, time, cycle, or budget boundary.
- [ ] **Step 4: Integrate fallback wakes and persisted agent-requested wake times with session start**; do not impose fixed session times or durations, and ensure pause/sleep tools stop before another autonomous turn.
- [ ] **Step 5: Run `python3 -m unittest prototype.tests.test_sessions prototype.tests.test_controller prototype.tests.test_appserver -v`** and confirm session and lifecycle tool tests pass.

### Task 5: Crash-safe in-flight recovery, queue deduplication, and observability

**Files:**
- Modify: `prototype/andrewbot/store.py`
- Modify: `prototype/andrewbot/sessions.py`
- Modify: `prototype/andrewbot/controller.py`
- Modify: `prototype/andrewbot/main.py`
- Modify: `prototype/tests/test_store.py`
- Modify: `prototype/tests/test_sessions.py`
- Modify: `prototype/tests/test_controller.py`

**Interfaces:**
- Consumes Task 1 `begin_cycle(session_id, cycle_id, local_day, reserved_cost_usd=0.0)` and `finish_cycle(cycle_id, status, actual_cost_usd=None)`.
- Startup recovery marks unfinished cycles interrupted and retains their reserved cost; it never replays an uncertain provider call.

- [ ] **Step 1: Add failing recovery tests** `test_interrupted_cycle_is_not_replayed_and_reserved_cost_is_retained`, `test_duplicate_telegram_update_does_not_queue_twice`, `test_uncertain_queued_replay_is_held_after_restart`, and `test_owner_replay_command_explicitly_requeues_uncertain_messages`; assert interrupted status, one retained reservation, no automatic duplicate provider call, and the explicit retry warning.
- [ ] **Step 2: Run `python3 -m unittest prototype.tests.test_store prototype.tests.test_sessions prototype.tests.test_controller -v`** and confirm recovery tests fail.
- [ ] **Step 3: Implement idempotent begin/finish records and startup reconciliation**; ensure uncertain requests keep the maximum reservation charged and queued user messages are replayed once only after successful completion.
- [ ] **Step 4: Extend `/status`, `/budget`, and activity records** with session state, next wake, daily cycle count, daily allowance, monthly spend, and reserve using safe summaries only.
- [ ] **Step 5: Run `python3 -m unittest prototype.tests.test_store prototype.tests.test_sessions prototype.tests.test_controller -v`** and confirm recovery, deduplication, and status tests pass.

### Task 6: Controlled validation and agent-directed schedule activation

**Files:**
- Modify: `prototype/andrewbot/main.py`
- Modify: `PHASE0_INSTALL_PLAN.md`
- Modify: `PHASE1_IMPLEMENTATION_PLAN.md`
- Modify: `ROOT_SESSION_HANDOFF.md`

**Interfaces:**
- Adds explicit configuration for the owner-confirmed fallback at 09:00 `America/Toronto` and agent-directed optional sessions.
- Keeps live autonomous sessions and unattended scheduling inactive until all project phases are complete.

- [ ] **Step 1: Add failing configuration tests** `test_fallback_wake_defaults_to_toronto_nine` and `test_validate_only_does_not_start_sessions_or_activate_schedule`; assert the configured timezone/time and that validation-only creates no session or model request.
- [ ] **Step 2: Run the targeted configuration tests** and confirm they fail before implementation.
- [ ] **Step 3: Add safe startup and activation interlocks**; validation-only must not start sessions; configuration must leave live autonomous scheduling disabled during Phase 2.
- [ ] **Step 4: Update the phase handoff documents** to record that Andrew selects optional work/sleep times, with 09:00 Toronto as the fixed fallback, and that live activation is deferred until all project phases are complete.
- [ ] **Step 5: Run `python3 -m unittest discover -s prototype/tests -v`** and confirm the complete prototype suite passes; do not start a live autonomous session or activate unattended scheduling in this phase.
