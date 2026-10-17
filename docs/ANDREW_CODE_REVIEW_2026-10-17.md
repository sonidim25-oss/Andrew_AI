# Andrew — deep code review and task backlog

Date: 2026-10-17 UTC  
Reviewed revision: `6d31d27b3fe9f9985b6bbad80cb32d359e309688` (`feature/andrew-multimodal`)  
Status: fixes in progress; per-task resolutions and verification are recorded below.

## Scope and verification

The review covered model gateway/App Server integration, controller command routing and delivery, worker/session lifecycle, media and avatar jobs, skill discovery/loading, backup/restore, runtime transport, and deployment entry points. Three independent review passes covered delivery/lifecycle, media/skills, and persistence/deployment; findings were consolidated and checked against their callers and existing tests.

Baseline verification passed:

- Core suite: **373 tests run, 2 optional skips**.
- Installer/protocol suite: **80 tests passed**, with the pinned isolated Letta runtime enabled.
- Additional isolated probes reproduced the behaviors described below. They used temporary databases, fake providers, mocked browser processes, and local subprocesses. No production state, real Telegram delivery, paid provider call, wake, or message replay was used.

Passing existing tests does not cover these missing scenarios. These are code-level findings, not a claim that every one has already occurred in production. Deliberately removed autonomous/code execution time limits and the intentional absence of spending authorization on deferred media messages are not treated as bugs.

## Priority summary

| Priority | Meaning | Open tasks |
| --- | --- | ---: |
| P0 — Extra high | Immediate uncontrolled loss, unauthorized execution, or equivalent critical behavior | 0 confirmed |
| P1 — High | Paid-request uncertainty, duplicate-delivery risk, or incorrect spending recovery | 0 |
| P2 — Medium | Broken supported operations, deployment gaps, or reproducible runtime races | 5 |
| P3 — Low | Avoidable inference cost or misleading capability handling | 1 |

No P0 finding was established; the empty category is not a guarantee that none exists. 6 tasks remain open.

## P0 — Extra high

No confirmed tasks at this priority.

## P1 — High

### REVIEW-001 — Stop automatic provider retries after an uncertain failure

- [x] **Name:** Make uncertain model-provider attempts non-retryable until reconciled.
- **Short description:** The gateway retains a spending reservation after a provider exception, but returns HTTP 502. The pinned Letta integration automatically retries that response and makes another provider request for the same user input. The first request may already have incurred a charge, so one input can cause additional paid attempts and multiple provider wait periods before the controller reports failure.
- **Location:** `prototype/andrewbot/gateway.py:113–122`; provider registration at `prototype/andrewbot/appserver.py:316–321`.
- **Short fix suggestion:** Propagate a distinct, non-retryable uncertain-attempt outcome, or disable upstream retries for this controller gateway. Persist a stable attempt/request identity and prevent another dispatch of that uncertain request. Keep the original reservation until reconciliation; do not release it merely to allow a retry.
- **Evidence:** An isolated run of the actual pinned Letta backend used a fake provider that raised `TimeoutError` on its first call and succeeded on its next call. One submitted user input produced **two provider calls**, equal user-message payloads, a successful reply from the second call, and **one unsettled reservation**. No owner retry was issued. Gateway-added budget context differed between attempts because the first reservation was retained.
- **Acceptance check:** With a provider timeout or invalid response after dispatch, a real pinned-runtime integration test observes exactly one provider attempt, an explicit uncertain outcome, and a retained reservation. A new attempt requires explicit reconciliation/authorization.

- **Resolution:** Provider uncertainty returns non-retryable HTTP 422 with a fixed safe category. Canonical request fingerprints persist in retained reservations and prevent duplicate dispatch across gateway recreation. Explicit owner replay rotates the authorization revision without releasing prior reservations.
- **Verification:** 376 core tests passed (2 optional skips); 83 script tests passed, including pinned Letta timeout and invalid-response cases proving exactly one provider attempt. Source fixed and tested; production deployment is separate.

### REVIEW-002 — Quarantine restored queues and reconcile spending before restart

- [x] **Name:** Add an operational recovery gate to snapshot restore.
- **Short description:** The feature named “memory restore” replaces the entire controller database, including operational queues and spending tables. Restoring an older snapshot can revive replies that were delivered after the snapshot and erase later settled charges. Those restored pending rows look unattempted to normal startup recovery and can be delivered again. The separate Letta database is not rewound with this snapshot.
- **Location:** `prototype/andrewbot/memory_backup.py:134–149`; restore entry point `prototype/andrewbot/memory_backup_cli.py:53–58`.
- **Short fix suggestion:** Restore into a mandatory recovery-halted state. Hold restored inbound work, outgoing deliveries, media jobs, and external-mutation attempts until their outcomes are reconciled. Preserve newer ledgers when available; otherwise require billing reconciliation before paid work resumes. Never interpret missing post-snapshot records as proof that actions or spending did not happen.
- **Evidence:** A temporary database was snapshotted with one pending reply; the reply was then removed as delivered and $1 of usage recorded. After restore, pending replies changed **0 → 1**, usage changed **$1 → absent**, and `claim_reply('normal')` immediately returned the restored reply. Existing integrity checks passed.
- **Acceptance check:** Restore a snapshot preceding confirmed sends and charges, then start the controller. No restored work is automatically sent or executed, and no model/media spending is admitted until recovery reconciliation completes. Saved memory remains readable.

## P2 — Medium

- **Resolution:** Restore now stages a recovery-halted database, quarantines inbox/reply/diary/media/avatar work, retains encrypted pre-restore evidence, and preserves verified newer reasoning/media ledgers, reservations and Telegram checkpoints. Startup, model admission and media admission respect a separate recovery gate. Administrator reconciliation requires an explicit confirmation and leaves old work held and Andrew paused. Updated the backup CLI/helper and operations guide.
- **Verification:** 380 core tests passed (2 optional skips); 83 pinned-fixture script tests passed; 42 installer tests and backup-helper shell syntax passed. Regressions cover already-delivered replies, retained charges/reservations/checkpoints, blocked clients and paid work, and held queues after reconciliation. Source fixed and tested; production deployment is separate.

### REVIEW-003 — Use one canonical definition of an owner control

- [ ] **Name:** Fix SQL/Python disagreement in control-message routing.
- **Short description:** The SQL selector builds its slash/button predicates before normalizing tabs and newlines, while Python strips those characters. SQL also compares buttons case-insensitively, while Python uses an exact button lookup. A newline-prefixed `/pause` is therefore skipped by both processing lanes. A case-variant button can repeatedly occupy the first control candidate and prevent a later `/pause` from reaching its handler while the model worker is busy.
- **Location:** `prototype/andrewbot/store.py:792–808`; `prototype/andrewbot/dispatch.py:142–170`.
- **Short fix suggestion:** Share canonical whitespace, command, and button normalization between persistence selection and dispatch classification. Ensure a selected-but-rejected candidate cannot repeatedly block later controls.
- **Evidence:** Temporary inbox fixture: `"\n/pause"` was classified as a control by Python, selected zero times by SQL, handled zero times, and remained pending. A second fixture with `"📊 status"` followed by `/pause` selected both IDs in SQL but handled neither during control processing.
- **Acceptance check:** With the model worker blocked, whitespace-prefixed controls and case-variant buttons follow a consistent policy, and a valid later `/pause` executes promptly without draining ordinary chat first.

### REVIEW-004 — Install skill assets through every supported deployment path

- [ ] **Name:** Package trusted skill documents with the controller everywhere.
- **Short description:** The incident installer includes the three Markdown documents, but the other deployment scripts install Python modules without those documents. Fresh installations advertise three skills that cannot be loaded. A later update through these paths can install a new document-hash manifest while leaving incompatible old Markdown. Controller startup does not detect the missing/mismatched assets.
- **Location:** `scripts/validate-and-start-controller.sh:38–41`; also `scripts/admin-setup.sh:148–153`, `scripts/activate-phase1.sh:47–50`, and `scripts/enable-browser-runtime.sh:257–258`.
- **Short fix suggestion:** Reuse the trusted asset planning/install/rollback logic in every entry point. Validate all skill documents as part of package acceptance before reporting startup success, and preserve/refuse unknown local changes consistently.
- **Evidence:** Recreating the Python-only deployed layout in a temporary directory caused `skills.list` to advertise all three skills while every `skills.load` failed with “Trusted skill document unavailable or changed.” Source inspection confirms these scripts do not install `/opt/andrew/skills`.
- **Acceptance check:** A fresh-install fixture and an upgrade fixture for each supported path can load all three exact expected documents. An interrupted upgrade restores matching loader/document versions. The currently deployed incident-installer path already includes these assets; this task addresses the other paths.

### REVIEW-005 — Allow a fresh approval when returning to an older avatar

- [ ] **Name:** Stop reusing terminal applied avatar proposals.
- **Short description:** `propose_avatar()` deduplicates proposals in the terminal `applied` state. After applying image A and then image B, requesting A again returns its old, already-applied proposal. That proposal cannot be approved again, so the supported flow cannot restore A even with fresh owner approval.
- **Location:** `prototype/andrewbot/media.py:353–370`, specifically the state filter at line 360.
- **Short fix suggestion:** Deduplicate only pending/in-flight proposals. Create a new proposal after a prior application has finished, retaining the requirement for fresh owner approval on every change.
- **Evidence:** Two temporary delivered PNG jobs were applied in sequence using local state transitions. Reproposing the first returned its original proposal ID; `approve_avatar()` returned `False`; `claim_avatar()` returned `None`. No Telegram or provider call occurred.
- **Acceptance check:** Apply A, apply B, propose A again: a new proposal is returned, nothing publishes before approval, and fresh approval permits exactly one application.

### REVIEW-006 — Bound the encoded worker response rather than only its raw streams

- [ ] **Name:** Make worker output limits compatible with the wire protocol.
- **Short description:** Each worker stream is bounded to 16 KiB before JSON encoding, but the outer runner accepts only 40 KiB for the entire encoded response. JSON escaping can expand otherwise permitted output beyond that limit. A successfully completed command is then reported as `worker_unavailable`, with its output discarded, obscuring whether its actions completed.
- **Location:** `runtime/worker_entry.py:93–124` and response serialization at `runtime/worker_entry.py:233`; outer capture/error mapping at `prototype/andrewbot/runner_service.py:124–136`.
- **Short fix suggestion:** Enforce a total budget on the final encoded response, including metadata and artifacts. Truncate streams safely to fit and report truncation separately from execution status; do not turn a completed command into an ambiguous worker-startup failure.
- **Evidence:** A local child wrote 16,384 NUL bytes to each stream, respecting both stream limits. The resulting valid response encoded to **196,693 bytes**, against the **40,960-byte** outer limit. Quotes/newlines can also increase encoded size; this is not limited to invalid UTF-8.
- **Acceptance check:** Exercise worst-case escaping on both streams and long artifact names. The response stays within the transport limit, retains correct completion/error status, and explicitly indicates truncation without rerunning the command.

### REVIEW-007 — Serialize browser reaping with request execution

- [ ] **Name:** Prevent the browser reaper from racing a new session.
- **Short description:** The reaper checks `handler.busy`, then schedules `process.close()` in another thread without reserving that busy state or locking the process. A request can start while cleanup is still running. Cleanup can then clear the newly created session's `name`, leaving a running browser process without its corresponding container-cleanup identity; other interleavings can close active streams.
- **Location:** `prototype/andrewbot/browser_service.py:24–49` and `prototype/andrewbot/browser_service.py:115–119`.
- **Short fix suggestion:** Use one lifecycle lock or serialized executor for `run()` and `close()`. Reserve admission before scheduling reaping, and clean up captured process/name pairs rather than clearing mutable fields that another request may have replaced.
- **Evidence:** A controlled interleaving using the actual `BrowserProcess.close()` and `run()` methods paused old-container removal, started and completed a new `tabs` request, then finished cleanup. The new process remained installed in `self.process`, but `self.name` changed from its new container name to `None`. Only the old container had been removed. Subprocess/container operations were mocked.
- **Acceptance check:** Block cleanup mid-flight and submit a new request concurrently. It waits or fails cleanly; after admission the active process and cleanup name still match. No active stream is closed by the idle reaper.

## P3 — Low

### REVIEW-008 — Reject disabled voice replies before invoking the model

- [ ] **Name:** Enforce voice-specific enablement at command admission.
- **Short description:** `/voice_reply` checks only the overall media flag. With the deployed image-only configuration, it still enters model inference even though voice generation is disabled and the eventual voice tool will reject it. This wastes reasoning budget and may produce a misleading reply instead of a clear local capability response.
- **Location:** `prototype/andrewbot/controller.py:112–117`; voice-specific enforcement currently happens later at `prototype/andrewbot/media.py:134–136`.
- **Short fix suggestion:** Check the requested capability's enablement before saving/claiming the model turn. Return a local “voice generation is disabled” response without invoking the model.
- **Evidence:** A temporary controller with `MediaPolicy(enabled=True, voice_enabled=False)` processed `/voice_reply Hello` and invoked the mocked model once.
- **Acceptance check:** Under image-only policy, `/voice_reply` invokes no model/provider and creates no media job or usage charge; `/image_reply` remains available through the existing authorization flow.

## Suggested implementation order

1. REVIEW-001 and REVIEW-002: uncertain paid requests and safe restoration.
2. REVIEW-003 and REVIEW-004: responsive controls and complete skill deployment.
3. REVIEW-006 and REVIEW-007: runtime result integrity and browser lifecycle.
4. REVIEW-005 and REVIEW-008: avatar reuse and capability admission.

Implementation should add the acceptance regressions listed above, preserve existing messages/memory/spending, and avoid replaying uncertain work. This review made no runtime fixes, commits, pushes, or deployments.
