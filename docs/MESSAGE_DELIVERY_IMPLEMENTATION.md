# Independent System Delivery Implementation Plan — P0

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for inline execution if this plan is approved. Steps use checkbox syntax for tracking.

**Status:** Installed on production at 23:31:49 UTC on 2026-10-16 after separate owner authorization. Source `6ebf056`, deployment preparation `25b1ffc`.

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

- [x] Write failing migration/restart tests covering each Review Focus crash point and preservation of all old rows, part progress and usage totals.
- [x] Run `PYTHONPATH=prototype:prototype/tests python3 -m unittest test_telegram_delivery -v`; confirm missing ownership/held-state behavior fails.
- [x] Implement additive migrations and transactional state transitions; test wrong-token completion and simultaneous claims on two Store connections.
- [x] Repeat the command; require all new state tests green before enabling another sender.

## Task 2: Hold uncertain sends

**Files:** `prototype/andrewbot/telegram.py`, `prototype/andrewbot/dispatch.py`, `prototype/andrewbot/diary.py`, `prototype/tests/test_telegram_delivery.py`, `prototype/tests/test_dispatch.py`.

**Interfaces:** Preserve `deliver_saved_message(...) -> bool`; when the next unacknowledged part has already been attempted, return incomplete without a network request. Persist held outbox/diary state on a send exception or restart recovery. Do not convert uncertain outgoing sends into model-input replay. Inspect existing explicit diary retry commands separately so intentional owner retries remain explicit.

- [x] Replace old automatic-retry expectations with failing assertions that only the first attempt reaches the fake API and subsequent dispatch iterations/restarts do not send again.
- [x] Add a final-ack crash test: delivery finalizes with no duplicate send; verify explicitly requested retries still require the owner path.
- [x] Run dispatch and delivery tests, implement held transitions, then require them to pass. Keep successful multipart delivery unchanged.

## Task 3: Two lanes with independent senders

**Files:** `prototype/andrewbot/store.py`, `prototype/andrewbot/dispatch.py`, `prototype/andrewbot/main.py`, `prototype/tests/test_dispatch.py`, `prototype/tests/test_latency_integration.py`.

**Interfaces:** Add optional `lane='normal'` to `queue_reply` and `complete_update`; accept only `normal` or `system`. Dispatch passes `system` only for authenticated local-control handling. Recovery notices, normal model replies and existing rows stay normal. Add one system sender alongside the existing normal sender; both use Task 1 claims instead of a shared delivery lock. Synchronous test operation services each lane without introducing model concurrency.

- [x] Write an event-controlled test blocking a normal send: enqueue `/status`, assert its system reply reaches the fake API before releasing the normal send; assert command order remains intact.
- [x] Add concurrent-claim, FIFO, forwarded/nonowner and lane-validation tests; snapshot metrics while both senders are active.
- [x] Observe failures before implementation, wire independent senders, then require all tests green.
- [x] Test shutdown while a sender is blocked: signal stop, stop admitting work, terminate senders before closing shared storage. If a sender cannot terminate within its bound, do not close storage it still owns; record a safe shutdown error rather than accessing a closed connection.

## Task 4: Regression checks and handoff

**Files:** `plan.md` and this plan's checkboxes.

- [x] Run the full core suite and pinned isolated script suite. Report deployment-manifest guard failures explicitly; do not update hashes merely to silence them.
- [x] Run `git diff --check` and read-only final review; confirm no model concurrency, production access, automatic uncertain-send replay or changed ledger totals.
- [x] Document completed source work and remaining worker, timeout, health and Gmail stages. Prepare deployment review separately, including the new latency module as an installation dependency.


## Implementation details and decisions

- Work continues inline in the existing checkout, as in the preceding increment;
  no existing state or unrelated changes are moved or discarded.
- `release_reply(reply_id, token)` returns definitely unsent work to pending on
  shutdown, retains uncertainty as held and requires the current claim token.
- Each lane admits only one active claim, including across Store connections.
  Network work never holds a Store/routing lock.
- Part attempts are atomically authorized once. A persisted `retry_notice` bit
  allows only the existing explicit owner diary retry to authorize its next part;
  acknowledged prefixes stay acknowledged. No outbox replay command is added.
- `close_controller_resources` keeps shared history/storage open when dispatch
  workers remain active after their bounded joins, preventing late writes to a
  closed connection. Main exits with a fixed error status in that case.
- Startup recovery runs before senders start, not when a Store connection opens.
  Opening a second connection must not steal a live sender's claim.

## Verification results

- 44 focused tests passed after RED→GREEN cycles for ownership, holding, priority
  routing, cross-connection exclusivity, known-unsent suffixes and shutdown.
- Core: 285 tests run, 283 passed, 2 optional skips (21.191 seconds).
- Scripts: 58 test methods run, 57 passed. Only
  `test_checked_in_deployment_manifest_matches_prepared_sources` fails, in five
  source-hash subtests (`diary.py`, `dispatch.py`, `main.py`, `store.py`, `telegram.py`).
  The manifest is not renewed as part of this source increment.
- Tests use local fixtures only. No production database/service, wake, replay,
  credential or paid provider request is used.


## Final review and deployment boundary

Independent review found no Critical or Important product-code defects. Its minor
migration-coverage gap was addressed with a fixture recreating both old schemas,
retaining an acknowledged prefix, an uncertain next part and nonzero usage.
Additional migration coverage preserves a queued message and memory record.

Deployment and rollback remain separate from this source increment. A controller
from before held-state support ignores the new fields and could replay uncertain
outgoing parts. A future installer must use a compatible rollback sender or leave
that service stopped rather than run old retry behavior over these records. The
old deployment manifest remains unchanged and rejects the new source as intended.
No existing production record was migrated or service restarted by this work.

Later model-worker, network-deadline, health and Gmail changes remain pending.
Diary sending still runs on its existing main-loop path; a separate diary worker
and automatic startup finalization of completed diary records are not introduced.
The existing explicit owner retry path continues to finalize such diaries without
resending acknowledged parts.

Final `git diff --check` passed. No commits, push or deployment were performed in this increment.

## Authorized deployment preparation

After the source commit `6ebf056`, the owner authorized deployment. The manifest
now includes the five reviewed delivery-source hashes and retains the prior
installed hashes for validation. The full core suite passed 283 tests with two
optional skips; all 59 script tests pass, including 31 installer tests.

Rollback changes protect held records: after attempting to launch the new sender,
any failure stops the controller and disables boot startup before restoring older
code or proof. It leaves that controller stopped and disabled rather than run a
sender that ignores held-state metadata. The database is never overwritten from
its snapshot. A failing disable command prevents restoring older sender code.
Before any new launch, the existing safe rollback/restart path remains available.

Independent review identified the rollback-ordering hazard. A regression asserted
that disabling precedes each old-module restoration; it failed before the order
fix and passed afterward. The author verified the corrected branch and full test
results; the reviewer's session ended before a final follow-up verdict.


## Deployment completed

The authorized installer completed at 23:31:49 UTC. Backup, SQLite snapshot,
acceptance results and `deployment-verification.json` are retained under
`/var/backups/andrew-incident-r7xl8a4o`. All server acceptance checks passed,
controller startup succeeded, module hashes match the renewed manifest, the
runtime proof validates, and the new outbox/progress schema is present.
Controller, Letta and runner services are active; controller boot startup remains
enabled because rollback was not needed. Saved Letta output allowance is 6,000.

Read-only exact-row comparisons against the pre-install snapshot confirmed
unchanged Telegram uncertainty/input records, messages, usage and reservations,
memory/personality data and references, diary entries, and research records.
Agent/conversation IDs and lifecycle/error state are unchanged. Four inputs remain
held; usage remains 80 rows totaling USD 0.111866056. No wake or replay was requested.
No new live message was sent to exercise priority delivery; that behavior is proved
by isolated tests, while live checks establish installation and data preservation.
The earlier source-only limits above were superseded by the separate deployment
request; later worker/deadline/health/Gmail work remains pending.
