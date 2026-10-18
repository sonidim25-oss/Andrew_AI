# Local Letta Durable Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resume newly journaled autonomous inputs from confirmed execution boundaries without repeating confirmed provider steps or tool effects.

**Architecture:** Build a separately verified extension of the pinned local Letta artifact. A private backend checkpoint store and exclusive local execution owner cooperate with controller dispatch receipts; explicit inspection and conditional resume operations replace attachment-based recovery. Unknown effects remain held.

**Tech Stack:** Existing Python 3 standard library/SQLite/unittest, Node ECMAScript modules/node:test, pinned Letta 0.34.13, existing SDK durable filesystem lock, Linux `flock` through a Python standard-library helper, systemd deployment conventions.

**Spec:** [2026-10-18-local-letta-durable-recovery-design.md](../specs/2026-10-18-local-letta-durable-recovery-design.md), approved by the owner on 2026-10-18.

## Global Constraints

- `@letta-ai/letta-code` **0.34.13**, base `letta.js` SHA-256 `eab90b26ded2647769a00ae29ec95fea1881173da53e2d54f620fc6df085b2fe`.
- Dedicated `local-recovery-v1` state under the existing writable Letta HOME; controller never reads private backend files.
- Default limits: 1 MiB checkpoint metadata, 16 MiB per payload blob, 256 MiB retained recovery data per conversation.
- Files are 0600, directories 0700; no credentials, auth headers or private reasoning in journals or logs.
- Lease lasts 15 seconds, renews every 5 seconds; an OS advisory execution lock prevents takeover while held, regardless of lease expiry.
- Preserve original logical input identity; new provider-step IDs identify new steps and charges. Never reuse process-local run counters.
- Unknown outcomes, legacy inputs, stale scope/configuration and pause/sleep/restore holds remain held. No absence-based success or replay inference.
- New autonomous inputs only; cloud recovery and owner-message recovery are outside this increment.
- No production service change, `/wake`, `/replay`, paid request, real delivery or publication during implementation/testing.
- Automatic resume stays disabled until the complete artifact/controller acceptance matrix passes. Installation is a separate final action.
- Every task uses red/green tests, self-review and a focused commit; do not include unrelated workspace changes.

## Review Focus

1. Provider success followed by loss of the backend response, including compaction: Task 6 must reuse exact settled gateway evidence without another charge.
2. Owner pause/revision change after resume admission but before dispatch: Tasks 6–7 must reject the delayed effect at its controller gate.
3. Backend restart with saved `started` input but no exact checkpoint: Tasks 2, 5 and 9 must hold without invoking old queue/approval recovery.
4. A second process or stale worker after lease expiry: Task 3 must prevent takeover while the OS lock remains held and reject the old fence afterward.
5. Crash after transcript projection or code rollback with outstanding admissions: Tasks 4 and 8 must avoid duplicate transcript operations and old-runtime input replay.

## Execution baseline and file boundaries

Worktree: `/home/daniel/andrew-autonomy-media`, branch `autonomy-media-20261017`.
An isolated linked worktree already exists. Preserve it and the confirmed deployed
media receipt changes currently uncommitted. The preimplementation baseline is
specification commit `6c8000f` plus those media changes. Task 0 baseline commit: `f7418d2`. Record the actual baseline
commit created in Task 0; do not mistake an earlier exported deployment for current
production source.

Implementation files are grouped by responsibility:

| Area | Files to create |
| --- | --- |
| Artifact build and pin | `letta_recovery/base.json`, `letta_recovery/patches.json`, `scripts/build-letta-recovery.py`, `letta_recovery/entry.mjs` |
| Backend contracts/storage | `letta_recovery/contracts.mjs`, `checkpoint-store.mjs`, `state-machine.mjs`, `payload-store.mjs` |
| Execution ownership | `letta_recovery/ownership.mjs`, `lock-helper.py` |
| Backend continuation | `letta_recovery/execution.mjs`, `transcript-projection.mjs` |
| App Server protocol | `letta_recovery/protocol.mjs` |
| Controller records/dispatch | `prototype/andrewbot/recovery_records.py`, `recovery_dispatch.py` |
| Controller coordinator | `prototype/andrewbot/recovery.py` |
| Deployment | `scripts/install-letta-recovery.py`, `scripts/letta-recovery-manifest.json`, `docs/local-letta-recovery-operations.md` |

Node tests live under `letta_recovery/tests/`; Python unit tests stay under
`prototype/tests` and `scripts/tests`. Real patched-artifact protocol checks stay
under `scripts/tests/test_local_recovery_protocol.py` and
`test_local_recovery_crash_protocol.py`. Existing modules receive narrow hooks;
do not reorganize the large controller/store/backend as unrelated refactoring.

### Shared contracts

Define these JSDoc types in Task 1 and use these exact names throughout:

- `Binding`: `{store_id, agent_id, conversation_id, session_id, cycle_id, client_message_id, consumer_id, input_sha, owner_revision, config_sha}`; fields are nonempty bounded strings, digests lowercase SHA-256.
- `Snapshot`: `{binding: Binding, revision: number, fence: number, phase: Phase, run_id: string, steps: object[], tools: object[], terminal: object|null}`; revisions/fences are nonnegative safe integers.
- `Phase`: `input_committed`, `provider_admitted`, `provider_confirmed`, `tool_prepared`, `tool_admitted`, `tool_confirmed`, `continuation_ready`, `terminal_committed`, `held`.
- `Claim`: `{claim_id, admission_id, owner_id, boot_id, fence, revision, expires_monotonic_ms}`.
- `ResumeIntent`: `{attempt_id, admission_id, binding: Binding, expected_revision, claim_id, fence}`.
- `Evidence`: `{status, binding, revision, fence, phase, hold_reason, result_digests}`; no raw payloads or tokens.
- `Admission`: `{admission_id, binding: Binding, kind, effect_id, request_sha, token, fence, state}`; tokens are opaque trusted-transport values, never model content.
- `DispatchGate.authorize(binding, claim, effect): Promise<Admission>` and `DispatchGate.result(admission): Promise<ResultEvidence>`; `effect` binds kind, stable step/tool ID and request digest. `Admission` carries an opaque token only to trusted local dispatch.
- `ResultEvidence`: `{state: 'confirmed'|'uncertain', admission_id, result_sha, result, usage}`; uncertain evidence has no invented result/usage.

Fixed hold categories: `legacy_unproven`, `binding_mismatch`, `unsupported_contract`,
`provider_uncertain`, `tool_uncertain`, `authority_lost`, `state_corrupt`,
`storage_limit`, `owner_blocked`, `configuration_changed`, `restore_required`,
`projection_conflict`. Unknown exceptions reduce to `state_corrupt`, never raw
exception text.

Each Node test uses temporary real directories and local test counters over
recorded fake gate invocations; `providerCountFor`, `fakeEffectCount` and Python
`calls_for`/`open_reservations_for` snippets denote those test-local counters or
SQLite reads, never production interfaces. Clock advancement is test injection;
real competing processes still own the OS locks.

Python uses validated plain dictionaries for the same wire fields. Centralize
validation in the records/bridge boundary instead of repeating ad hoc coercions.

## Task 0: Establish a reproducible deployed-media baseline

**Files:** modify `scripts/tests/test_owner_timeout_patch.py`; preserve and commit only the already deployed media changes listed by `git status` at execution start.

**Interfaces:** consumes existing `patch_source(data)`; produces an honest passing fixture for the current `LettaAgent.reply` signature and a named baseline commit. No runtime timeout change.

- [x] **Step 1:** Run `PYTHONPATH=prototype:. python3 -m unittest discover -s prototype/tests -q` and `python3 -m unittest discover -s scripts/tests -q`. Record actual totals. Reproduce the known timeout fixture's 1 failure/5 errors before changing it; investigate any new failure.
- [x] **Step 2:** Repair `OwnerTimeoutPatchTests.source(timeout)` to substitute the timeout default by AST byte span while preserving `origin`; change its expected patched source comparison to replace only that default span. Keep tests for 120, 600, `None`, missing unlimited socket support, acceptance failure and rollback. The existing failing patcher tests are the red evidence.
- [x] **Step 3:** Run `python3 -m unittest discover -s scripts/tests -p test_owner_timeout_patch.py -q`; expect seven tests OK. Run the complete scripts suite; protocol skips without the pinned binary must be reported as skips.
- [x] **Step 4:** Verify media manifest hashes against the exact source, run `git diff --check`, review the deployed media diff and commit those files plus the fixture repair. Record the SHA in this plan. Do not commit unrelated files or change services.

## Task 1: Pin and build an inactive recovery artifact

**Files:** create build/pin files, `letta_recovery/contracts.mjs`, `letta_recovery/tests/artifact.test.mjs`, `scripts/tests/test_letta_recovery_builder.py`, and `letta_recovery/tests/protocol_fixture.py`.

**Interfaces:** produces `validateBinding(value): Binding`, `validateSnapshot(value): Snapshot`, contract constant `local_durable_recovery_v1`, and `build_release(base_package: Path, output: Path) -> dict`. `entry.mjs` exports `createLocalRecovery({sdk, root, enabled, gate})`; at this task it cannot execute or advertise the capability. `sdk` exposes existing `acquireDurableFileLock`, typed executor/transcript callbacks and safe protocol sends; later tasks fill implementations.

- [x] **Step 1:** Write `test_build_refuses_modified_or_ambiguous_base` and `test_build_is_repeatable_and_leaves_npm_package_unchanged`. Assert wrong SHA/multiple anchors fail, two builds have identical release file hashes, and the base tree is byte-identical. Add Node `invalid_binding_rejects_missing_identity` with `assert.throws(() => validateBinding({}), /binding/)`.
- [x] **Step 2:** Run `python3 -m unittest discover -s scripts/tests -p test_letta_recovery_builder.py -q` and `node --test letta_recovery/tests/artifact.test.mjs`; expect the named missing builder/contracts behavior to fail.
- [x] **Step 3:** Implement the builder and validators. Read the full base package and dependency lock; copy a self-contained release dependency layout so relocation cannot resolve a service-writable or unrelated package. Preserve base license/metadata. Build hooks from unique reviewed source-section anchors, retaining readable patch definitions. Output a manifest containing base/output hashes and contract version.
- [x] **Step 4:** Implement `protocol_fixture.py` as a test helper with `launch_release(release, data_dir, provider) -> Fixture` and `Fixture.{client, provider_calls, provider_calls_for(effect_id), stop(), kill(), restart()}`. It uses temporary HOME/backend/SQLite and loopback fake providers only. Add a startup test proving the inactive artifact makes no provider/tool calls and advertises no recovery capability.
- [x] **Step 5:** Run both targeted commands and startup fixture; expect OK and identical build hashes. Commit the exact Task 1 files.

## Task 2: Implement durable checkpoints and exact private payloads

**Files:** create backend storage modules and `letta_recovery/tests/checkpoint-store.test.mjs`; modify contracts as needed without changing shared field names.

**Interfaces:** `new CheckpointStore({root, acquireLock, limits})`; `inspect(binding): Evidence`; `load(binding): Snapshot|null`; `commit(binding, expectedRevision, transition, payloads): Snapshot`. `putPayload(bytes): {sha, size}` and `readPayload(sha): Buffer`. `transition(snapshot, event): Snapshot` rejects illegal phase/identity changes. Root/store identity is created only by explicit initialization/admission, never inspection.

- [x] **Step 1:** Write real-filesystem tests `missing_inspection_does_not_create_state`, `commit_survives_process_exit`, `stale_revision_cannot_overwrite`, `corrupt_or_missing_blob_holds` and `size_and_unsafe_file_limits_hold`. Assert exact limits `1048576`, `16777216`, `268435456`, modes `0o600`/`0o700`, no writes from inspection, and rejection of a symlink/FIFO/digest mismatch. Assert `started` without a new checkpoint returns `legacy_unproven`, never `continuation_ready`.
```javascript
// missing_inspection_does_not_create_state; temporary parent exists, root does not.
assert.equal(store.inspect(binding).status, 'missing');
assert.equal(existsSync(root), false);
// stale_revision_cannot_overwrite
assert.throws(() => store.commit(binding, oldRevision, event, []), /revision/);
assert.deepEqual(store.load(binding), latestCommitted);
```

- [x] **Step 2:** Run `node --test letta_recovery/tests/checkpoint-store.test.mjs`; verify missing storage/state-transition behavior fails.
- [x] **Step 3:** Implement blob-first fsync then atomic checkpoint replacement under the SDK lock. Validate all identity fields, byte limits, safe integers and nine legal phases. Failed post-effect persistence leaves the previous admission uncertain. Use one authoritative conversation checkpoint with retained admission/identity tombstones; do not silently rotate its store UUID.
- [x] **Step 4:** Run the same tests with subprocess termination at blob-write and checkpoint-replace boundaries. Expect only the previous or next valid checkpoint, never success from temporary files. Commit the storage modules/tests.

## Task 3: Add exclusive local execution ownership

**Files:** create `ownership.mjs`, `lock-helper.py`, `letta_recovery/tests/ownership.test.mjs`.

**Interfaces:** `new OwnershipManager({store, lockHelper, clock, bootId})`; async `claim(binding, expectedRevision, admissionId): Claim`, `renew(claim): Claim`, `release(claim): void`, `revoke(claim): void`; `assertOwned(claim): void` before every effect. `lock-helper.py --path PATH` holds kernel `fcntl.flock(LOCK_EX|LOCK_NB)`, reports acquisition on stdout, releases on parent pipe EOF or explicit release, and never runs SDK work.

- [ ] **Step 1:** Write `live_os_lock_blocks_expired_lease_takeover`, `parent_exit_releases_execution_lock`, `stale_fence_cannot_dispatch`, `boot_change_invalidates_lease` and `clock_jump_cannot_extend_authority`. Use two real processes; assert lease `15000` ms and renewal `5000` ms. Advancing the lease clock cannot override a held OS lock.
```javascript
// live_os_lock_blocks_expired_lease_takeover; two real owner processes.
clock.advance(15001);
await assert.rejects(second.claim(binding, revision, 'second-admission'), /owned/);
assert.equal(fakeEffectCount(), 0);
// stale_fence_cannot_dispatch
assert.throws(() => first.assertOwned(oldClaim), /authority/);
```

- [ ] **Step 2:** Run `node --test letta_recovery/tests/ownership.test.mjs`; verify absence of exclusive/fenced ownership is the failure.
- [ ] **Step 3:** Implement the helper and manager using a parent-owned pipe, process-instance UUID, OS boot ID and monotonic deadlines. Reserve/increment fences durably under the checkpoint lock; never identify an owner by PID alone. Loss of helper/connection/renewal prevents new dispatch. Release a claim without deleting evidence.
- [ ] **Step 4:** Run the real competing-process and parent-kill tests, including a stale worker trying to commit a result after a successor claim. Expect rejection without successor overwrite. Commit these files.

## Task 4: Journal normal execution and idempotent transcript projection

**Files:** create `execution.mjs`, `transcript-projection.mjs`, `letta_recovery/tests/execution.test.mjs`, `projection.test.mjs`; extend reviewed build patches for backend/store/executor sections.

**Interfaces:** `new RecoveryExecution({store, ownership, gate, sdk})`; async `admitInput(binding, input): Snapshot`, `advance(binding, claim): Snapshot`; `projectTranscript(snapshot): void`; `collectSettledPayloads(binding, controllerEvidence): number` deletes only unreferenced payloads after bound terminal acknowledgement and accounting settlement, retaining deduplication/fence tombstones. The SDK adapter provides `executeProviderStep(request, admission)`, `emitConfirmedToolResult(call, result)`, `readProjectionOperations(scope)` and `replaceProjection(scope, rows)`; the replacement atomically retains operation IDs/digests. All provider calls, including compaction, cross `DispatchGate`.

- [ ] **Step 1:** Write `confirmed_response_is_not_requested_again`, `confirmed_tool_result_is_not_executed_again`, `admitted_unknown_effect_holds`, `projection_commit_gap_does_not_duplicate` , `compaction_keeps_unfinished_payloads` and `unacknowledged_or_unsettled_payloads_are_not_collected`. Use literal fake response/result payloads and real persistent checkpoints. Assert one dispatch for a confirmed step and zero new dispatches for an unknown admitted step.
```javascript
// confirmed_response_is_not_requested_again; restart after durable confirmation.
await recovered.advance(binding, replacementClaim);
assert.equal(providerCountFor(originalStepId), 1);
// projection_commit_gap_does_not_duplicate
assert.equal(projectedRows.filter(row => row.operation_id === operationId).length, 1);
// admitted_unknown_effect_holds
assert.equal(store.load(binding).phase, 'held');
assert.equal(providerCountFor(unknownStepId), 1);
```

- [ ] **Step 2:** Run `node --test letta_recovery/tests/execution.test.mjs letta_recovery/tests/projection.test.mjs`; confirm execution/projection behavior fails before implementation.
- [ ] **Step 3:** Integrate `LocalBackend`/`HeadlessBackend` and `LocalStore` only for adopted recovery conversations. Allocate random run/step/tool IDs before dispatch. Persist accepted input before acknowledgement, provider response before tool preparation, exact tool return before continuation, and terminal before delivery. Prevent all legacy effectful entry points from bypassing these gates.
- [ ] **Step 4:** Implement typed checkpoint-to-transcript projection with stable operation IDs and digests in its atomic persisted rows. Recover projection intent by exact identity, never transcript text. A conflicting existing operation digest holds. Retain terminal data and compaction inputs until acknowledged/settled; garbage collection requires exact current controller evidence and cannot remove unresolved or still-referenced blobs.
- [ ] **Step 5:** Run the Node tests plus an actual built-artifact fake-provider turn using Task 1's fixture. Assert saved transcript operations and run identity survive kill/restart. Commit the executor/projection integration and tests.

## Task 5: Expose inspection and conditional recovery protocol

**Files:** create `protocol.mjs`, `letta_recovery/tests/protocol.test.mjs`, `scripts/tests/test_local_recovery_protocol.py`; modify entry/build patches for App Server router/startup.

**Interfaces:** `handleRecoveryCommand(command, connection): Promise<Response>` validates binding/version and routes `recovery_inspect`, `recovery_claim`, `recovery_resume`, `recovery_status`, `recovery_subscribe`, `recovery_result_read`, `recovery_revoke`; claim renewal/release retain exact identities. Resume uses `ResumeIntent` and returns `{attempt_id, state, revision}`. Capability requires all store/owner/executor/protocol dependencies and remains feature-gated.

- [ ] **Step 1:** Write protocol tests `inspection_and_handshake_are_evidence_only`, `lost_resume_reply_reuses_attempt`, `subscription_does_not_pump_unrelated_queue`, `old_consumer_cannot_settle_new_input` and `legacy_started_without_checkpoint_is_held`. Assert exact attempt IDs, one continuation and zero startup/inspection effects or recovery-state writes.
```python
# lost_resume_reply_reuses_attempt; real patched fixture and persisted identities.
self.assertEqual(first_status['attempt_id'], repeated_status['attempt_id'])
self.assertEqual(fixture.provider_calls_for(next_step_id), 1)
# inspection_and_handshake_are_evidence_only
self.assertEqual(before_recovery_tree, after_recovery_tree)
self.assertEqual(fixture.provider_calls, [])
```

- [ ] **Step 2:** Run Node protocol tests and `ANDREW_LOCAL_RECOVERY_TEST_RELEASE=RELEASE ANDREW_LETTA_TEST_BINARY=RELEASE/letta.js PYTHONPATH=prototype:. python3 -m unittest discover -s scripts/tests -p test_local_recovery_protocol.py -q`; the executor substitutes the actual temporary release path. Expect missing new protocol behavior to fail, not skip.
- [ ] **Step 3:** Implement explicit commands. Revalidate claim/resume revisions under lock; commit resume intent before effect execution. Duplicate attempt IDs return status; changed payloads conflict. Inspection/result/status reads do not create runtimes, repair projections, acknowledge terminals or create lock files.
- [ ] **Step 4:** Disable eager interrupted-work recovery, terminal promotion/cleanup and queue pumping at launch/handshake for the extension. Reject legacy mutations on adopted conversations; leave legacy read access and nonadopted behavior available without silently adopting old inputs.
- [ ] **Step 5:** Run both protocol suites against the actual patched bundle, including malformed fields, changed binding and the local App Server identity. Confirm cloud `conn-` guards were not relaxed. The two new recovery protocol suites require `ANDREW_LOCAL_RECOVERY_TEST_RELEASE` to name the verified release directory; without it they skip explicitly, and recovery acceptance treats such skips as failure. Commit protocol integration/tests.

## Task 6: Make controller provider/tool outcomes durable and scoped

**Files:** create `recovery_records.py`, `recovery_dispatch.py`, `prototype/tests/test_recovery_dispatch.py`; modify `prototype/andrewbot/store.py` schema initialization, `gateway.py` dispatch, `controller.py` tool boundary, and `appserver.py` trusted callback transport (all four under `prototype/andrewbot`).

**Interfaces:** `RecoveryRecords(store)` creates additive `recovery_attempts`, `recovery_effects`, `recovery_results` tables under Store's lock. Methods: `admit_effect(binding, effect) -> dict`, `confirm_provider(admission_id, response, usage) -> dict`, `confirm_tool(admission_id, result) -> dict`, `effect_result(admission_id) -> dict`, `revoke(admission_id) -> None`. `RecoveryDispatch.authorize(binding, claim, effect) -> dict` and `result(admission_id) -> dict` implement the shared gate contract.

- [ ] **Step 1:** Write Python tests `test_gateway_success_backend_loss_reuses_response`, `test_unknown_provider_keeps_original_reservation`, `test_compaction_uses_scoped_receipts`, `test_pause_between_admission_and_dispatch_blocks_call`, `test_duplicate_confirmed_tool_returns_exact_result` and `test_unknown_tool_does_not_execute_twice`. Persist actual SQLite and reopen it; use subprocess exit after response/result commits. Assert exact fixture payload/digest, reservation count and fake destination invocation counts.
```python
# gateway_success_backend_loss_reuses_response
self.assertEqual(reopened.effect_result(admission_id)['result'], fixture_response)
self.assertEqual(provider.calls_for(step_id), 1)
self.assertEqual(open_reservations_for(step_id), 0)
# unknown_provider_keeps_original_reservation
self.assertEqual(open_reservations_for(unknown_step_id), 1)
self.assertEqual(provider.calls_for(unknown_step_id), 1)
```

- [ ] **Step 2:** Run `PYTHONPATH=prototype:prototype/tests:. python3 -m unittest test_recovery_dispatch -q`; observe failure from the absent durable gate/receipt behavior.
- [ ] **Step 3:** Add schema initialization and records wrapper without changing existing receipt meanings. Provider confirmation stores the exact validated response and settles usage/removes only that reservation in one SQLite transaction. Admission is committed before dispatch; lost/unknown outcomes retain their existing reservation. Do not store provider auth headers or reasoning fields.
- [ ] **Step 4:** Add trusted local admission metadata to backend↔gateway and tool callbacks, outside model content, stripping it before provider forwarding. Opaque tokens are scoped to binding, owner revision, effect digest and fence. Recheck current owner controls/configuration and reservation admission immediately before dispatch; old tokens cannot authorize another step. Confirmed repeat calls return exact saved results; unresolved repeats return a fixed hold. Preserve backwards-compatible call signatures by adding keyword-only `recovery=None` to `ModelGateway.complete` and `Controller.execute_tool`; trusted protocol wrappers supply it, model tool arguments cannot.
- [ ] **Step 5:** Run targeted tests, gateway/core tool regressions and the actual patched protocol fixture. Verify normal owner replies retain their current path, recovery metadata is absent from fake provider payloads, and no global reservations are cleared. Commit controller/gateway receipt changes.

## Task 7: Coordinate recovery through the existing model worker

**Files:** create `recovery.py`, `prototype/tests/test_local_recovery.py`; modify `appserver.py`, `sessions.py`, `main.py`, `workers.py`, `configuration.py`, and `scripts/diagnose-session.py`.

**Interfaces:** `LocalRecoveryClient` provides `inspect(binding)`, `claim(binding, revision, admission_id)`, `resume(intent)`, `status(attempt_id)`, `subscribe(attempt_id)`, `result_read(admission_id)`, `revoke(attempt_id)`. `RecoveryCoordinator(store, controller, client)` provides `inspect_pending() -> list[dict]`, `reconcile_one() -> bool`, `cancel_active() -> None`. Configuration flag `ANDREW_LOCAL_RECOVERY_ENABLED` defaults to `false`.

- [ ] **Step 1:** Write `test_ready_boundary_continues_original_input_once`, `test_unknown_outcome_stays_held`, `test_lost_resume_reply_is_inspected_by_original_attempt`, `test_pending_owner_input_preempts_recovery`, `test_pause_sleep_restore_and_revision_races_block_next_effect`, and `test_terminal_recovery_preserves_held_messages`. Assert unchanged original input ID, no normal session dispatch while recovery runs and no unrelated hold/queue/reservation changes.
```python
# ready_boundary_continues_original_input_once
self.assertEqual(recovered_binding['client_message_id'], original_input_id)
self.assertEqual(provider.calls_for(continuation_step_id), 1)
# pause_sleep_restore_and_revision_races_block_next_effect
self.assertEqual(fake_destination.calls, [])
self.assertEqual(store.get_state(control_key), '1')
```

- [ ] **Step 2:** Run `PYTHONPATH=prototype:prototype/tests:. python3 -m unittest test_local_recovery -q`; confirm missing coordinator behavior fails.
- [ ] **Step 3:** Implement additive recovery attempts and local protocol bridge. Negotiate version/store identity. Select only newly admitted autonomous cycles with exact current binding/configuration. Reconcile positive evidence first; any unknown effect/billing outcome blocks resume. Reuse the existing serialized model worker and saved conversation, not `send_user_turn`, `runtime_start` or `sync`.
- [ ] **Step 4:** Route owner controls to revoke the active admission, cancel transport and prohibit subsequent dispatch while preserving already admitted effects. Complete a turn only through the existing exact bound terminal-receipt contract. Diagnostics expose fixed categories/counts/IDs, never payloads or opaque tokens; preserve legacy no-receipt labels.
- [ ] **Step 5:** Run targeted/core worker/session tests and the real patched protocol test. Verify default-disabled startup still makes no attachment/resume call and performs no legacy queue recovery. Commit coordinator/bridge/diagnostic changes.

## Task 8: Package activation, coordinated snapshots and safe rollback

**Files:** create installer/manifest/operations documentation and `scripts/tests/test_letta_recovery_installer.py`; modify the existing installer module lists/manifests for new controller modules and `systemd/andrew-letta.service` only through a reviewed override selected by the new installer. Extend `prototype/andrewbot/memory_backup.py` and its tests for paired snapshot metadata; existing controller-only backups stay valid but insufficient to release recovery holds.

**Interfaces:** `build_candidate(backend_release, deployed_sources, backup_dir) -> Candidate`, `verify_candidate(candidate) -> Evidence`, `activate(candidate, enable_resume=False)`, `rollback(snapshot)`. A candidate binds backend/controller hashes, capability version and runtime proof; no runtime defaults enable recovery. Snapshot metadata includes both store UUIDs and digests and requires quiescent workers.

- [ ] **Step 1:** Write `test_unknown_deployment_rejected_before_service_change`, `test_resume_disabled_during_initial_activation`, `test_restore_either_store_requires_reconciliation`, and `test_rollback_never_pumps_new_contract_input`. Simulate service operations with existing installer conventions and run rollback startup against a disposable actual older binary. Assert outstanding admissions remain held; unsafe rollback leaves services stopped.
```python
# rollback_never_pumps_new_contract_input
self.assertEqual(older_runtime.provider_calls, [])
self.assertEqual(retained_admission['state'], 'held')
# unsafe exclusion must never start the old service.
self.assertNotIn(('start', 'andrew-letta.service'), unsafe_rollback.service_actions)
```

- [ ] **Step 2:** Run `python3 -m unittest discover -s scripts/tests -p test_letta_recovery_installer.py -q` and targeted memory-backup tests; confirm missing candidate/restore/rollback guarantees fail.
- [ ] **Step 3:** Implement exact artifact/controller validation and merge preservation before service mutation. Update every changed module's tested hash using verified source. Select a root-owned separate artifact without mutating npm. Initial install keeps automatic resume off and preserves owner pause/sleep and all unresolved records.
- [ ] **Step 4:** Implement quiescent paired snapshot/restore holds and rollback fence. Never start an older backend on live new-contract inputs: use an isolated retained snapshot only after verifying exclusion of all newer admissions from its input-disposition/queue stores. Otherwise leave services stopped and retain journals. Document explicit operator commands for install preview, apply, read-only diagnosis and rollback; do not execute them on production.
- [ ] **Step 5:** Run installer, backup and disposable older-runtime rollback checks; verify no production path or service was touched. Commit packaging and operations changes.

## Task 9: Complete the process-loss matrix and readiness report

**Files:** create `scripts/tests/test_local_recovery_crash_protocol.py`, extend both local recovery protocol fixtures and document final results in `Memory update.md`/`docs/session-recovery.md`.

**Interfaces:** consumes the actual built artifact, coordinator, dispatch receipts and installer candidate from Tasks 1–8. Produces a reproducible acceptance report with source/artifact/candidate hashes, named test totals and remaining limitations; not an activation request until tests pass.

- [ ] **Step 1:** Write parameterized subprocess-kill scenarios for before/after each admission, provider confirmation, tool effect/result commit, transcript projection and terminal commit. Include backend-only, controller-only and simultaneous loss, lost transport responses, concurrent owners, boot/fence changes, quotas/corrupt files, compaction, owner-control races and paired restore/rollback. Each scenario asserts literal provider/tool/next-step counts and exact original identity.
```python
# Useful unfinished-turn recovery, not terminal-only recovery.
self.assertEqual(calls_for(confirmed_provider_step), 1)
self.assertEqual(calls_for(confirmed_tool_attempt), 1)
self.assertEqual(calls_for(new_continuation_step), 1)
self.assertEqual(resumed_input_id, original_input_id)
# Companion unknown-outcome case.
self.assertEqual(unknown_checkpoint['phase'], 'held')
self.assertEqual(calls_for(unknown_attempt), 1)
```

- [ ] **Step 2:** Run `ANDREW_LOCAL_RECOVERY_TEST_RELEASE=RELEASE ANDREW_LETTA_TEST_BINARY=RELEASE/letta.js PYTHONPATH=prototype:. python3 -m unittest discover -s scripts/tests -p test_local_recovery_crash_protocol.py -q` using the real temporary release. Confirm each fault scenario reaches its intended boundary; a test that never crashes or never crosses the boundary is not acceptance.
- [ ] **Step 3:** Resolve failures in their owning task modules without weakening hold semantics. Demonstrate one useful unfinished-turn recovery from a confirmed response plus confirmed tool result to exactly one new step. Its paired unknown-provider/tool case must remain held with zero retries.
- [ ] **Step 4:** Run `node --test letta_recovery/tests/*.test.mjs`, the full core unittest suite, all scripts tests with both release environment variables set, all compatible protocol checks against the pristine pinned binary with the recovery-release variable unset (report new-suite skips explicitly), and installer acceptance on the exact merged deployment candidate. Use explicit actual release paths, not the `RELEASE` notation used here. Report skips and unrelated failures by name; no blanket passing claim.
- [ ] **Step 5:** Compare changed source hashes to deployment manifests; run `git diff --check`; request whole-change review and address critical/important findings. Update the roadmap with verified local readiness, undeployed status and known nonrecoverable outcomes. Commit final tests/docs. Produce the concrete installer command only after this evidence exists; do not install, wake or replay as part of implementation.

## Plan self-review and handoff

All specification sections map to Tasks 1–9: artifact pinning (1), identity and
persistence (1–2), ownership (3), effect boundaries/projection/compaction (4, 6),
protocol/startup (5), controller authorization and accounting (6–7), retention/
restore/deployment/rollback (2, 4, 8), full acceptance matrix (9). Task 0 repairs a
known baseline test fixture and preserves deployed media work before new changes.
The five Review Focus failures each have named tests in their owning tasks.

Shared names above are the interfaces later tasks consume. No task enables live
recovery by itself; all intermediate components are independently testable with
isolated storage and fake effects. Native execution is recommended because the
backend, protocol and controller gates share a tightly coupled contract. The
implementation method and this plan still require owner review before code work.
