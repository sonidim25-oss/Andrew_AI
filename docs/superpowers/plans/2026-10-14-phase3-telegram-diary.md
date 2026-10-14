# Phase 3: Telegram Diary and Owner Views Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an immutable first-person diary and reliable owner-only Telegram views while preserving objective activity records and the all-phases activation gate.

**Architecture:** Store diary entries, validated activity references, and delivery state in SQLite, separate from messages and objective activity. A dedicated diary helper validates content and formats owner views; the trusted controller exposes a narrowly scoped save tool and commands, while the session runner and Telegram loop create incident summaries and deliver committed entries through a conservative outbox.

**Tech Stack:** Python 3.12, SQLite, `zoneinfo`, existing Letta App Server client and Telegram long-poll API.

**Spec:** `docs/superpowers/specs/2026-10-14-phase3-telegram-diary-design.md`

**Execution note (2026-10-14):** Native implementation changes are in place. Tests were not added or run in this pass. A fresh static review reported no concrete blockers. This workspace has no Git metadata; no commit was made, consistent with the owner's “git later” instruction.

## Global Constraints

- Do not enable autonomous sessions, daily fallback, or the controller service; keep `ALL_PROJECT_PHASES_COMPLETE = False`.
- Diary dates use `America/Toronto`; timestamps are persisted in UTC.
- Normal diary entries are first-person, immutable, and unique per Toronto-local date; maximum content length is 3,000 Unicode characters.
- Diary rows are separate from messages, objective activity, cycle, and spend records.
- Telegram delivery becomes `uncertain` before network dispatch and `delivered` only after Telegram confirms success; uncertain sends require explicit owner retry.
- Never deliver credentials, private reasoning, or unsafe diary content. Do not log entry text.
- Keep all tests offline; no live Telegram credentials, live Letta requests, or autonomous sessions.
- Do not commit; the owner deferred Git.

## Review Focus

- Concurrent or repeated saves for one Toronto date: Task 1 tests that the unique daily constraint admits one normal entry.
- Invalid event references or secret-like content: Tasks 1–2 test all-or-nothing rejection without lifecycle mutation.
- Crash after Telegram send begins: Task 4 tests that restart leaves an uncertain delivery instead of silently sending a duplicate.
- Restart during an active session: Task 4 tests a factual labeled incident summary, not a first-person diary.
- Diary commands issued by non-owners or for malformed dates: Task 3 tests no private content is returned.

---

### Task 1: Durable diary records and event references

**Files:**
- Modify: `prototype/andrewbot/store.py`
- Create: `prototype/tests/test_store_diary.py`

**Interfaces:**
- Produces `Store.create_diary_entry(local_date: str, content: str, record_type: str = "diary", session_id: str = "", event_ids: list[int] = ()) -> str`, returning a generated entry ID. Validate referenced activity row IDs and insert the entry plus references atomically. Enforce one `diary` row per `local_date`; incident summaries may coexist and are system-authored.
- Produces `Store.diary_entry(local_date: str | None = None) -> dict | None`, `Store.diary_entries(limit: int = 7) -> list[dict]`, and delivery methods `claim_pending_diary_delivery(entry_id: str) -> bool`, `mark_diary_delivered(entry_id: str) -> bool`, and `retry_uncertain_diary_delivery(entry_id: str) -> bool`.
- Changes `Store.add_activity(event: str) -> int` to return the inserted objective activity row ID without changing its existing write behavior.

- [ ] **Step 1: Write failing store tests** `test_diary_entry_persists_separately_from_messages_and_activity`, `test_only_one_normal_diary_entry_per_local_day`, `test_diary_event_references_must_exist_atomically`, and `test_diary_schema_migrates_existing_database`; assert exact records, preserved pre-migration rows, uniqueness, and zero partial rows after an invalid reference.
- [ ] **Step 2: Run `python3 -m unittest prototype.tests.test_store_diary -v`** and confirm failures on missing schema/API.
- [ ] **Step 3: Implement the diary schema and methods** with foreign-key-like validation in one SQLite transaction; preserve existing database content and never expose an update method for diary text.
- [ ] **Step 4: Run `python3 -m unittest prototype.tests.test_store_diary -v`** and confirm all store tests pass.

### Task 2: Diary validation and formatting helpers

**Files:**
- Create: `prototype/andrewbot/diary.py`
- Create: `prototype/tests/test_diary.py`

**Interfaces:**
- Produces `validate_diary_content(content: str) -> str`, accepting non-empty plain text up to 3,000 Unicode characters and rejecting secrets/private-reasoning markers using explicit patterns; reject rather than mutate unsafe text.
- Produces `format_diary_entry(entry: dict) -> str` and `incident_summary(session_id: str, reason: str, activity: list[dict]) -> str`; incident text must be clearly labeled, factual, and generated only from safe event summaries.
- Produces `parse_diary_command(text: str) -> tuple[str, str | None]` for `/diary`, `/diary YYYY-MM-DD`, and `/diary retry YYYY-MM-DD`, validating calendar dates strictly.

- [ ] **Step 1: Write failing helper tests** `test_rejects_secret_patterns_without_returning_original`, `test_rejects_reasoning_markers_and_over_limit_text`, `test_formats_diary_without_activity_or_chat_transcript`, `test_incident_summary_is_labeled_and_uses_only_safe_events`, and `test_diary_command_dates_are_strict_calendar_dates`.
- [ ] **Step 2: Run `python3 -m unittest prototype.tests.test_diary -v`** and confirm the helper module is missing.
- [ ] **Step 3: Implement the validators and formatters** with fixed safe error messages and no logging of rejected content.
- [ ] **Step 4: Run `python3 -m unittest prototype.tests.test_diary -v`** and confirm all helper tests pass.

### Task 3: Agent save tool and owner-only diary commands

**Files:**
- Modify: `prototype/andrewbot/appserver.py`
- Modify: `prototype/andrewbot/controller.py`
- Modify: `prototype/tests/test_appserver.py`
- Modify: `prototype/tests/test_controller.py`

**Interfaces:**
- Adds strict read-only tool `recent_activity` returning up to eight objective event IDs and safe summaries, plus strict external tool `save_diary_entry` with required `content` and optional integer `event_ids`; no filesystem or message-sending capability is exposed.
- Adds `Controller.execute_tool("save_diary_entry", arguments)` which validates content, uses current Toronto-local date and persisted active session ID, validates event references through `Store`, and does not modify sleep/pause state.
- Implements `/diary`, `/diary YYYY-MM-DD`, and `/diary retry YYYY-MM-DD`; only uncertain entries may be explicitly retried. `/diary` reads diary rows only; `/activity` remains objective and displays event IDs.
- Refuses unauthorized, forwarded, group, or invalid-date requests without returning diary content; existing private owner authorization remains the boundary.

- [ ] **Step 1: Write failing tests** `test_runtime_advertises_recent_activity_and_diary_save_tools`, `test_save_diary_tool_stores_local_date_and_rejects_bad_references_without_state_change`, `test_diary_command_shows_latest_and_requested_date_only`, `test_diary_retry_only_requeues_uncertain_entry`, and `test_non_owner_or_forwarded_update_cannot_read_diary`.
- [ ] **Step 2: Run the targeted controller and App Server tests** and confirm the new expectations fail.
- [ ] **Step 3: Implement the tool schema, prompt guidance, controller handlers, and help text**; add instructions against invented facts, secrets, chat transcript copying, and private chain-of-thought.
- [ ] **Step 4: Run the targeted controller and App Server tests** and confirm all relevant cases pass.

### Task 4: Session wrap-up, incident recovery, and Telegram delivery outbox

**Files:**
- Modify: `prototype/andrewbot/sessions.py`
- Modify: `prototype/andrewbot/main.py`
- Modify: `prototype/andrewbot/telegram.py` only if an explicit safe delivery result contract is needed
- Modify: `prototype/tests/test_sessions.py`
- Modify: `prototype/tests/test_telegram.py`
- Modify: `PHASE0_INSTALL_PLAN.md`
- Modify: `PROJECT_DESCRIPTION.md`

**Interfaces:**
- Adds session instructions to save the diary before voluntary sleep/normal completion. No extra model call is made for diary generation.
- On provider failure or startup recovery of an interrupted session, create one factual `incident_summary` only if there is no diary for that Toronto-local date; do not create one for owner pause, normal voluntary sleep, or schedule disablement.
- Adds `deliver_pending_diary(telegram, store, owner_id) -> int`: atomically move pending to uncertain before network dispatch, mark delivered only after a successful Telegram response, and leave uncertain on any exception. Do not automatically retry uncertain records.
- Main loop delivers pending rows after restart and after a newly saved record, only to the configured owner ID. Messages fit Telegram's size limit and contain no logs or chat history.
- Update phase documentation to distinguish implemented dormant diary support from live activation; do not change the source activation gate.

- [ ] **Step 1: Write failing integration tests** `test_voluntary_sleep_can_save_diary_without_an_extra_turn`, `test_restart_marks_interrupted_session_with_labeled_incident_summary`, `test_incident_is_not_created_for_owner_pause_or_normal_sleep`, `test_delivery_is_persisted_before_send_and_success_is_recorded`, and `test_uncertain_delivery_survives_restart_without_automatic_retry`.
- [ ] **Step 2: Run targeted session and Telegram tests** and confirm the new cases fail.
- [ ] **Step 3: Implement wrap-up prompting, safe incident creation, and conservative outbox delivery**; preserve the existing 4,000-character Telegram truncation contract and ensure diary output is below the limit before formatting.
- [ ] **Step 4: Run the targeted session and Telegram tests**, then `python3 -m unittest discover -s prototype/tests -v`; confirm the complete offline prototype suite passes.
- [ ] **Step 5: Confirm source activation gate remains false and no service/session was started.**

## Final Review

- Inspect the full diff for private content in logs, accidental diary edits, unauthorized delivery, and any path that enables autonomous operation.
- Confirm SQLite migration retains existing messages, activity, usage, lifecycle state, and cycle data.
- Provide implementation and verification evidence; do not claim live Telegram delivery or autonomous behavior was tested.
