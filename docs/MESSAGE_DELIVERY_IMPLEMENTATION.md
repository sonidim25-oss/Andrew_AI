# Independent System Delivery Implementation Plan — P0

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for inline execution if this plan is approved. Steps use checkbox syntax for tracking.

**Status:** Prepared for the next increment; not implemented or approved for execution yet.

**Goal:** Deliver local owner-control replies while ordinary Telegram delivery is blocked, without automatic replay of uncertain sends.

**Architecture:** Two durable outbox lanes, each with one sender, share transactional ownership and per-part acknowledgement records. First introduce held delivery state and recovery; only then enable the second sender. Owner-command execution remains ordered.

**Tech Stack:** Python, SQLite, unittest, existing Telegram API.

**Spec:** `docs/MESSAGE_LATENCY_DESIGN.md`, second increment.

## Global Constraints

- Preserve messages, IDs, memory, spending and existing working-tree changes.
- Do not automatically replay uncertain inputs or deliveries.
- No production wake, paid request, installation or manifest renewal.
- Existing outbox rows default to the normal lane. Existing acknowledged parts remain acknowledged.
- Preserve FIFO within each lane; a system reply may overtake a normal reply.
- Do not hold a database or routing lock during network calls.

## Review Focus

- A process dies after sending but before acknowledgement: hold the attempted part on restart.
- A process dies after its final acknowledgement: complete locally without sending again.
- Two sender attempts overlap: only one can own a given delivery.
- An old row has attempts but no part progress: conservatively hold it.
- A blocked normal sender must not delay local command execution or system sending.

## Task 1: Durable ownership and held-state migration

**Files:** `prototype/andrewbot/store.py`, `prototype/tests/test_telegram_delivery.py`.

**Interfaces:** Add outbox `lane TEXT NOT NULL DEFAULT 'normal'`, `delivery_state TEXT NOT NULL DEFAULT 'pending'`, and nullable `claim_token TEXT`. Add `claim_reply(lane) -> dict | None`, `hold_reply(reply_id, token)`, `finish_reply(reply_id, token)` and `recover_reply_claims() -> int`. Claims atomically select the oldest pending row in the lane and assign an unpredictable token; completion and hold require that token. Existing uncertainty is inferred from attempted, unacknowledged progress, including legacy attempts without progress. A claim without any send attempt is safely returned to pending. A completed final part is finalized locally. Never infer unsent merely from an absent acknowledgement.

- [ ] Write failing migration/restart tests covering each Review Focus crash point and preservation of all old rows, part progress and usage totals.
- [ ] Run `PYTHONPATH=prototype:prototype/tests python3 -m unittest test_telegram_delivery -v`; confirm missing ownership/held-state behavior fails.
- [ ] Implement additive migrations and transactional state transitions; test wrong-token completion and simultaneous claims on two Store connections.
- [ ] Repeat the command; require all new state tests green before enabling another sender.

## Task 2: Hold uncertain sends

**Files:** `prototype/andrewbot/telegram.py`, `prototype/andrewbot/dispatch.py`, `prototype/andrewbot/diary.py`, `prototype/tests/test_telegram_delivery.py`, `prototype/tests/test_dispatch.py`.

**Interfaces:** Preserve `deliver_saved_message(...) -> bool`; when the next unacknowledged part has already been attempted, return incomplete without a network request. Persist held outbox/diary state on a send exception or restart recovery. Do not convert uncertain outgoing sends into model-input replay. Inspect existing explicit diary retry commands separately so intentional owner retries remain explicit.

- [ ] Replace old automatic-retry expectations with failing assertions that only the first attempt reaches the fake API and subsequent dispatch iterations/restarts do not send again.
- [ ] Add a final-ack crash test: delivery finalizes with no duplicate send; verify explicitly requested retries still require the owner path.
- [ ] Run dispatch and delivery tests, implement held transitions, then require them to pass. Keep successful multipart delivery unchanged.

## Task 3: Two lanes with independent senders

**Files:** `prototype/andrewbot/store.py`, `prototype/andrewbot/dispatch.py`, `prototype/andrewbot/main.py`, `prototype/tests/test_dispatch.py`, `prototype/tests/test_latency_integration.py`.

**Interfaces:** Add optional `lane='normal'` to `queue_reply` and `complete_update`; accept only `normal` or `system`. Dispatch passes `system` only for authenticated local-control handling. Recovery notices, normal model replies and existing rows stay normal. Add one system sender alongside the existing normal sender; both use Task 1 claims instead of a shared delivery lock. Synchronous test operation services each lane without introducing model concurrency.

- [ ] Write an event-controlled test blocking a normal send: enqueue `/status`, assert its system reply reaches the fake API before releasing the normal send; assert command order remains intact.
- [ ] Add concurrent-claim, FIFO, forwarded/nonowner and lane-validation tests; snapshot metrics while both senders are active.
- [ ] Observe failures before implementation, wire independent senders, then require all tests green.
- [ ] Test shutdown while a sender is blocked: signal stop, stop admitting work, terminate senders before closing shared storage. If a sender cannot terminate within its bound, do not close storage it still owns; record a safe shutdown error rather than accessing a closed connection.

## Task 4: Regression checks and handoff

**Files:** `plan.md` and this plan's checkboxes.

- [ ] Run the full core suite and pinned isolated script suite. Report deployment-manifest guard failures explicitly; do not update hashes merely to silence them.
- [ ] Run `git diff --check` and read-only final review; confirm no model concurrency, production access, automatic uncertain-send replay or changed ledger totals.
- [ ] Document completed source work and remaining worker, timeout, health and Gmail stages. Prepare deployment review separately, including the new latency module as an installation dependency.
