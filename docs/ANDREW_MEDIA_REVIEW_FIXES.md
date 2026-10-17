# Andrew media review fixes

Scope: code-review findings 2, 3, and 4, in that order. Preserve existing changes, messages, memory, spending records, and generated samples. No uncertain message replay, paid provider tests, deployment, or avatar publication is part of this repair work.

Finding 1 (rechecking reduced budgets for queued jobs) remains outside this requested repair list and is still an enablement blocker.

## 2. Stop media admission before waiting for workers

Status: fixed in source; regression tests pass.

Problem: controller shutdown currently waits for model and diary workers before signaling the media worker. During that interval, media can start another paid generation, Telegram delivery, or approved avatar update.

Repair:
- Give workers a nonblocking stop signal separate from waiting for completion.
- Signal all workers before joining any of them, retaining the existing shared shutdown deadline.
- Preserve already attempted work and held outcomes; leave unstarted jobs queued.

Verification:
- Reproduce the dispatch-level shutdown window using an isolated store and fake provider/Telegram adapters.
- Verify that generation, delivery, and avatar application cannot start while shutdown waits for another worker.
- Verify that an in-flight call remains accounted for without closing storage beneath it.

## 3. Protect image references atomically against cleanup

Status: fixed in source; regression tests pass.

Problem: editing and avatar proposals validate their source before saving the reference. Retention cleanup can delete an old source between validation and insertion. An accepted edit then fails before a provider call and incorrectly holds a spending reservation.

Repair:
- Validate and protect the reference within the same database transaction/lock as inserting the edit or avatar proposal.
- Keep retention checks serialized with those reference writes.
- Classify failures known to occur before any provider attempt separately from uncertain provider outcomes, releasing unspent reservations.

Verification:
- Reproduce cleanup racing an edit and an avatar proposal with deterministic thread coordination.
- Confirm accepted references survive cleanup, or reject the request before acceptance/reservation.
- Confirm no provider call or uncertain spending hold for a missing reference detected before generation.

## 4. Report held delivery failures safely

Status: fixed in source; regression tests pass.

Problem: media delivery failures are held silently. No owner notice is queued and no useful error category is stored. Telegram exceptions currently discard HTTP status and failure category.

Repair:
- Preserve only fixed error categories and HTTP status; never retain tokens, full URLs, prompts, authorization headers, or raw provider responses.
- Persist the held outcome and queue one local owner notice atomically.
- Apply the same one-time notice policy to interrupted deliveries recovered at startup.
- Keep all uncertain sends held without automatic retry.

Verification:
- Cover HTTP rejection, network/timeout failure, malformed response, and restart recovery with mocks.
- Confirm one notice per held attempt, persistent sanitized diagnostics, and no repeat Telegram send.
- Run core and script regression suites and refresh deployment source hashes.

## Implementation record

- Finding 2: `ModelWorker` and `MediaWorker` now expose a nonblocking stop signal. Dispatch signals all workers before any join. Media checks again after artifact preparation/conversion and returns unattempted claims to their pending state. In-flight provider calls can settle without closing storage underneath them.
- Finding 3: edit/ avatar reference validation and durable insertion share an immediate SQLite transaction. Cleanup takes the same write transaction, including across separate store connections. Known preparation failures before a provider attempt now use `failed / preflight_failed` and release the unused reservation.
- Finding 4: Telegram media failures carry fixed categories and optional HTTP status. Holding a failed or interrupted delivery and queuing its system notice share one transaction. Repeated recovery cannot repeat a notice or send. The live sample runner uses the same hold path.
- Verification so far: all 17 focused repair regressions passed; all 28 existing media tests and the sample-runner replay regression passed. Full regression results will be recorded below.

The changes are prepared in source and remain undeployed. No production state, messages, spending records, or media artifacts were modified during these repairs. Finding 1 remains unresolved as explicitly excluded from this requested list.

## Final verification

All three selected findings (2, 3, 4) are fixed in source, in the requested order.

- Core suite: 362 tests run; 360 passed and two optional tests skipped.
- Script suite: 73 passed, including the pinned isolated Letta media protocol and sample replay checks.
- Focused repair regressions: 17 passed; the reviewer independently reran these and the 28 existing media tests.
- Focused independent review: no reportable issues in repairs 2–4. Finding 1 remains the previously documented enablement blocker.
- All 20 deployment source hashes match, and `git diff --check` passes.

No commit, push, deployment, paid provider call, Telegram resend, or production database change was performed during this repair task.

## Deployment

Source `81ee6be`, including repairs 2–4, was deployed and verified on 2026-10-17. All 20 installed hashes match, all four services are active, and all 33 database tables and both sample artifacts were preserved. Media remains disabled pending finding 1. Evidence: `/var/backups/andrew-incident-mlgyoqvh/media-deployment-verification.json`.

## Finding 1 — owner-authorized image enablement follow-up

The owner subsequently requested image generation be enabled. Queued jobs now recheck current permission, daily/monthly aggregate spend including outstanding reservations, per-job caps, and estimated cost against the saved reservation in the claim transaction. A disallowed unstarted job is cancelled with a local owner notice and no provider call; uncertain attempts remain held/accounted. Four regression tests cover reductions, combined reservations, a voice-rate increase, and image-only enablement. The earlier enablement blocker is resolved in source, pending this rollout's production verification.
