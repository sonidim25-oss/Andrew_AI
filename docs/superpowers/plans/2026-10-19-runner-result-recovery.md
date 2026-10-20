# Runner Result Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for recommended native sequential execution, or superpowers:subagent-driven-development if the owner selects that method. Execute task by task after plan review.

**Goal:** Recover the exact successful response of a newly journaled autonomous Python, Node, or shell operation without executing it again.

**Architecture:** A receiver ledger saves an authenticated, encrypted result before the private runner socket replies. The controller imports that evidence into its existing tool and research records; a narrow backend transition confirms an exact held tool. Dispatch remains subject to existing owner and uncertainty guards.

**Tech Stack:** Python 3, SQLite FULL synchronization, Linux Unix sockets/SO_PEERCRED/SCM_CREDENTIALS/flock, existing cryptography AES-GCM, Node.js recovery backend, unittest/node:test, systemd encrypted credentials.

**Spec:** [Approved runner result recovery design](../specs/2026-10-18-runner-result-recovery-design.md), approved by the owner on 2026-10-19.

**Execution:** Native sequential execution recommended because the eight tasks share identities and transaction boundaries. Owner directed continuation after diary installation on 2026-10-19. Native sequential implementation is active in `/home/daniel/andrew-runner-receipts`; see `../../runner-result-recovery-implementation.md` for the checkpoint. Production activation is a separate final step after reviewable artifacts and acceptance.


Implementation checkpoint: Tasks 1–6 are implemented/tested in the isolated checkout.
No task commits or production deployment have occurred. Live environment-flag and
adoption wiring belongs to Task 8; the adapter is unmounted by default.

## Global constraints

- Only newly journaled autonomous `python.run`, `node.run`, `shell.run`; never retrofit the legacy uncertain Python attempt or automatically replay messages.
- Contract `runner_result_recovery_v1`, version `1`, formatter `runner_text_v1`, backend capability `held_tool_result_v1`.
- Preserve every existing binding field and canonical digest. Operation IDs are random 256-bit lowercase hex; controller/runner store IDs persist across restarts.
- Worker request limit 64 KiB, envelope 80 KiB, metadata document 8 KiB, worker response 40 KiB plus 8 KiB metadata; length-prefixed frames reject unknown fields/trailing frames.
- Receipt directory `/var/lib/andrew/runtime/runner-receipts`, owner `andrew-runtime`, directory 0700/files 0600; never mount it or its credential into workers.
- Dedicated 32-byte credential `andrew-runner-receipts-key`; AES-GCM fresh 96-bit nonce, operation identity and contract as associated data. No raw code, key, grant or output in logs/diagnostics.
- Capacity 256 MiB, 50,000 records, reserve 64 KiB per new admission. No unresolved eviction, operation-ID reuse or automatic expiry; lookup works at capacity.
- `prepared` is distinct from runner `admitted`. Only accepted-grant admission can start one worker. Reconnection never dispatches an existing operation.
- Only validated `status: ok`, `safe_error: null`, verified cleanup and durable result commit qualify for successful import.
- Missing, prepared, admitted-without-result, error, corrupt and ambiguous results remain held. A prepared lookup alone never authorizes cancellation or another execution after controller grant admission.
- Historical import never clears pause/sleep/owner revision, restore fences, unrelated uncertainty or spending reservations. Further execution requires normal current authority.
- Preserve deployed command normalization, media guards, owner-message handoff and local-control routing during merges.
- Feature defaults disabled. Use isolated fake workers/destinations; no paid inference or real publication. No production installation during implementation.

## Review focus

1. Owner input/pause between receiver `ready` and controller admission: zero worker calls, only exact prepared mapping/legacy attempt cancelled — Tasks 3–4.
2. Worker success followed by cleanup failure or slow receipt commit: no importable success/no second execution — Tasks 2–3.
3. Receiver result exists but research focus/owner revision changes before import: preserve original linkage and controls — Task 5.
4. Result was imported while the backend remains held or crashes during confirmation: consume exact evidence without callback/replay — Tasks 5–6.
5. Partial rollout, older restore or changed store/key identity: retain evidence and refuse execution — Tasks 7–8.

## File structure and common types

New focused modules:
- `prototype/andrewbot/recovery_identity.py`: existing canonical identity functions extracted without behavior changes; `recovery_records.py` re-exports existing names.
- `prototype/andrewbot/runner_protocol.py`: strict envelopes, immutable `OperationIdentity`, `DispatchGrant`, `ReceiptEvidence`, typed safe protocol errors.
- `prototype/andrewbot/runner_format.py`: shared pure request preparation/response validation/formatter.
- `prototype/andrewbot/runner_receipts.py`: encrypted receiver ledger and single-process owner lock.
- `prototype/andrewbot/runner_recovery.py`: controller mapping, grant admission, evidence import and lookup orchestration.
- `scripts/install-runner-recovery.py`: coordinated candidate/build/activate/enable entry point using existing installer helpers.
- `scripts/setup-runner-receipts-key.py`: protected encrypted-credential provisioning, no execution.

`Binding` is the existing strict ten-field dictionary. `OperationIdentity` is a frozen dataclass containing `contract`, `version`, `controller_store_id`, `runner_store_id`, `binding`, `binding_sha`, `admission_id`, `effect_id`, `request_sha`, `operation_id`, `legacy_attempt_id`, `tool_name`, `worker_request_sha`, `formatter`, `research_context`. `research_context` is an immutable document with `session_id`, `cycle_id`, `research_id`, `research_revision`.

`DispatchGrant` contains `operation_sha`, `challenge`, `scope_id`, `fence`, `boot_id`, `expires_monotonic_ms`. Grants are transient and connection-bound.

`ReceiptEvidence` contains `contract`, `version`, `identity`, `identity_sha`, `state`, `response`, `response_sha`, `cleanup_verified`; response fields are null unless a durably stored bounded response exists. States: `missing`, `prepared`, `admitted`, `uncertain`, `result`. A strict validator enforces the state-dependent shape; error receipts may retain bounded response evidence but cannot qualify for import.

## Task 1: Shared identity, wire validation and formatter

**Files:** Create the identity/protocol/format modules above; modify `recovery_records.py`, `runner_client.py`, `tool_runtime.py`; add `prototype/tests/test_runner_protocol.py` and `test_runner_format.py`.

**Interfaces:**
- Preserve `canonical(value) -> str`, `digest(value) -> str`, `validate_binding(dict) -> dict`, `valid_id(value) -> bool` through existing imports.
- `prepare_runner_request(name: str, arguments: dict) -> dict` for the three eligible tools.
- `validate_runner_response(response: dict) -> dict` returns a new normalized value, never mutates its argument.
- `format_runner_response(response: dict, *, version: str = 'runner_text_v1') -> str` retains the existing 15,000-character limit and untrusted-output suffix exactly.
- `validate_identity(value: dict) -> OperationIdentity`, `validate_grant(value: dict) -> DispatchGrant`, `validate_evidence(value: dict, expected: OperationIdentity) -> ReceiptEvidence`.
- `encode_frame(document: dict, *, limit: int) -> bytes` and bounded synchronous/async frame readers sharing the same size/schema rules; legacy worker frames retain their current format.

- [x] Write tests: `test_canonical_hashes_unchanged` pins Unicode, float and nested identity digests against current recovery fixtures; `test_formatter_parity` compares existing Python/Node/shell output byte-for-byte; `test_response_validation_is_pure` asserts the original input remains unchanged.
- [x] Write `test_unknown_fields_and_oversized_documents_rejected` for 64/80/8/40+8 KiB boundaries; `test_metadata_never_enters_worker_request` asserts prepared request keys remain exactly `{type,code}` or `{type,command}`.
- [x] Run `PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -p 'test_runner_protocol.py' -v`; confirm the new behavior fails before implementation.
- [x] Implement these interfaces and route existing normal result formatting through the shared pure formatter without enabling receipts.
- [x] Run the new protocol/format tests plus existing runner-client/tool-runtime/recovery-dispatch tests; run `node --test letta_recovery/tests/*.test.mjs` to verify canonical compatibility.
- [ ] Commit only this task's files in the isolated implementation checkout: `refactor: share runner protocol validation and result formatting`.

## Task 2: Durable encrypted receiver ledger

**Files:** Create `runner_receipts.py`, `prototype/tests/test_runner_receipts.py`, and `prototype/tests/fixtures/runner_receipt_process.py`.

**Interfaces:**
- `RunnerReceiptStore(root: Path, key: bytes, *, expected_uid: int, capacity_bytes: int = 268435456, max_records: int = 50000)` acquires an exclusive process-owner lock and validates secure paths/credential identity.
- `provision(*, controller_store_id: str) -> str` initializes an explicitly new store and returns its persistent runner UUID; ordinary startup must not silently provision a missing identity.
- `prepare(identity: OperationIdentity) -> PrepareResult`, `admit(identity: OperationIdentity) -> ReceiptEvidence`, `finish(identity: OperationIdentity, response: dict, *, cleanup_verified: bool) -> ReceiptEvidence`, `mark_uncertain(identity: OperationIdentity) -> None`.
- `lookup(identity: OperationIdentity) -> ReceiptEvidence`, `capabilities() -> dict`, `checkpoint() -> None`, `close() -> None`.
- `PrepareResult` is a frozen `{created: bool, evidence: ReceiptEvidence}` value; only `created=True` can enter a new connection handshake. Immutable identity rows, state changes and result ciphertext/digest are transactional. `prepare` reserves capacity; `admit` requires its exact prepared row; duplicate operations never grant execution. `finish` must verify an identical existing result or reject divergence.

- [x] Write tests: commit/reopen returns identical UUID/response/digest; duplicate changed identity raises a fixed error; missing/existing corrupt identity is never reprovisioned; wrong key/UID/symlink/FIFO refuses startup.
- [x] Add subprocess faults before/after admission and result commit; saved evidence survives, incomplete admission never becomes success. Inject commit/disk-full failures and assert no unpersisted success escapes.
- [x] Add `test_capacity_blocks_prepare_but_not_lookup`, `test_no_unresolved_eviction`, `test_owner_lock_blocks_second_process`, `test_no_plaintext_code_output_or_key_in_store`, `test_cleanup_failure_is_not_importable_success`.
- [x] Run `PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -p 'test_runner_receipts.py' -v` and observe failures before implementation.
- [x] Implement with SQLite `synchronous=FULL`, DELETE journal, secure no-follow opens and parent fsync, existing AESGCM dependency. Capacity checks include database allocation/metadata and 64 KiB reservation padding; failed commit prevents dispatch.
- [x] Re-run this suite with real reopened stores and terminated subprocesses; verify secret sentinels do not appear in metadata/log output.
- [ ] Commit: `feat: persist encrypted runner execution receipts`.

## Task 3: Authenticated socket handshake and execution ownership

**Files:** Modify `runner_service.py`, `runner_client.py`; extend `test_runner_service.py`, `test_runner_client.py`; create `prototype/tests/test_runner_receipt_protocol.py`.

**Interfaces:**
- Extend `RunnerSocketServer(runner, *, receipts=None, controller_uid=None, receipts_enabled=False)`; default preserves the legacy path.
- `RunnerClient.receipt_capabilities(*, expected_runner_uid: int) -> dict`.
- `RunnerClient.execute_receipted(request: dict, identity: OperationIdentity, *, expected_runner_uid: int, grant_factory: Callable[[dict], DispatchGrant], cancel_event=None) -> ReceiptEvidence`.
- `RunnerClient.lookup_receipt(identity: OperationIdentity, *, expected_runner_uid: int, timeout_seconds: float = 5) -> ReceiptEvidence`.
- Runner verifies controller UID through accepted-socket SO_PEERCRED; client enables SO_PASSCRED and verifies actual runtime-sender SCM_CREDENTIALS with recvmsg for every received chunk/frame. Reject missing/truncated credentials; close/reject unexpected passed file descriptors. Systemd listener-creator credentials alone are insufficient ([Linux documentation](https://man7.org/linux/man-pages/man7/unix.7.html)). `grant_factory` receives exact ready identity/challenge after receiver preparation; invocation grants no authority by itself.
- Server persists prepared, sends ready, validates connection-bound grant/lease/boot/challenge, commits admitted, starts one worker, verifies cleanup, saves result, then replies. Existing operation submission only returns evidence; it cannot invoke a grant factory or launch a worker.

- [x] Write real-socket tests for wrong peer UIDs, missing/truncated sender credentials, inherited listener handles, duplicate prepare returning created=False, stale/foreign-boot grants, altered challenge/digest, repeated grant, extra frames, disconnect before grant (execution count zero), and duplicate submit/reconnect (count at most one).
- [x] Add `test_busy_through_cleanup_and_receipt_commit`: block the receipt save after a completed worker; second execute is rejected, while lookup/capabilities remain available. Releasing save permits owner release only after durable commit.
- [x] Add `test_disconnect_after_completion_still_saves_result`, `test_cancel_during_execution_retains_unknown`, `test_success_response_with_failed_cleanup_stays_uncertain` and legacy/browser frame compatibility tests.
- [x] Run `PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -p 'test_runner_receipt_protocol.py' -v`; observe failures.
- [x] Implement private protocol routing, lease checks and transient per-connection challenges. Remove future-callback ownership release on this path; the handler owns cleanup plus persistence. Lookup does not call runner execution, container recovery or admission methods.
- [x] Run the new real-socket suite and all existing runner concurrency/client tests. Assert receipt files/credential never appear in `_podman_args` mounts.
- [ ] Commit: `feat: add authenticated runner receipt handshake and lookup`.

## Task 4: Controller preparation and fenced dispatch adapter

**Files:** Create `runner_recovery.py`; modify `store.py`, `controller.py`, `recovery_dispatch.py`, `recovery_records.py`, `tool_runtime.py`; create `prototype/tests/test_runner_recovery_dispatch.py`.

**Interfaces:**
- `RunnerRecoveryAdapter(store, runner_client, *, expected_runner_uid: int, runner_store_id: str)`.
- `prepare(metadata: dict, name: str, arguments: dict) -> OperationIdentity` records one mapping/legacy attempt and saved research context while the effect stays prepared.
- `grant(metadata: dict, identity: OperationIdentity, ready: dict) -> DispatchGrant` revalidates live scope/owner/lease and atomically calls normal tool admission plus marks the mapping dispatch-admitted.
- `cancel_prepared(identity: OperationIdentity) -> None` fences only a controller-prepared effect/mapping and sets only its linked legacy attempt `not_executed`; no grant can subsequently be issued for that mapping.
- `execute(metadata: dict, name: str, arguments: dict) -> str` uses Task 3 exactly once and the same evidence import path as Task 5 for successful normal responses.
- Tables `runner_operations` and `runner_imports`: unique operation/admission/legacy IDs; immutable identity JSON/hash and original research context; preparation/admission/import states. Schema initialization is idempotent.
- Configuration flag `ANDREW_RUNNER_RECEIPTS_ENABLED=0` by default; pin `runner_receipt_store_id` explicitly. Enabling requires receiver capability/store pin and backend capability; no unjournaled fallback for an adopted mapping.

- [x] Write `test_mapping_commits_before_rpc_and_identity_is_reused`, `test_no_recursive_legacy_attempt`, `test_wrong_store_or_missing_capability_never_dispatches` and an immutable-identity mutation test.
- [x] Inject owner input, pause, sleep, revision change, restore and lease expiry between ready/grant; assert zero runner calls. Prepared owner cancellation produces exactly one `not_executed` legacy outcome and cannot reopen the operation.
- [x] Add a failure after controller grant admission but before send: mark held, never reuse the operation, never infer a result from receiver preparation. Existing legacy/owner-message tools keep their routes.
- [x] Run `PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -p 'test_runner_recovery_dispatch.py' -v`; observe failures.
- [x] Implement the adapter path before generic `begin_tool`/recursive callback for eligible autonomous mappings only. Preserve cancellation context and untrusted provenance. Do not hold store transactions across RPC.
- [x] Run the new suite plus `test_recovery_dispatch`, `test_owner_handoff`, `test_controller`, `test_tool_runtime`; check operation/attempt counts and unchanged owner state directly.
- [ ] Commit: `feat: bind runner receipt dispatch to controller authority`.

## Task 5: Atomic historical result import and bounded lookup

**Files:** Extend `runner_recovery.py`, `recovery_records.py`, `store.py`, `recovery.py`, `memory.py`; create `prototype/tests/test_runner_result_import.py`.

**Interfaces:**
- `RunnerRecoveryAdapter.import_result(identity: OperationIdentity, evidence: ReceiptEvidence) -> str` returns the exact model-facing formatted result after one transaction.
- `reconcile_pending_runner_result(store, runner_client, binding: dict, *, expected_runner_uid: int) -> bool` performs at most one existing-operation lookup with a five-second deadline per model-worker pass.
- `Store.record_runner_research_result(attempt_id: str, context: dict, *, outcome: str) -> None` records original saved research linkage idempotently; do not use current-focus redirect in `record_research_tool`.
- `RecoveryRecords.effect_result` adds `runner_receipt` metadata only for exact imported successful evidence: contract, operation/store IDs, identity/response digests, formatter and `cleanup_verified: true`. Normal proofs are otherwise unchanged.
- Successful import atomically confirms exact recovery effect/result, marks linked legacy attempt returned, saves immutable import evidence and one research event, and preserves untrusted status.

- [x] Write exact formatter/hash parity and import-twice tests; assert one legacy settlement/research observation, zero callback/runner execution and no duplicate result rows.
- [x] Add focus/revision changes before import, owner pause/sleep/pending message/restore flag, and unrelated unknown effects/reservations: historical evidence may import, owner controls remain byte-for-byte unchanged and continuation remains blocked.
- [x] Add changed operation/admission/request/response/store/formatter, error response, cleanup false, malformed evidence, missing/prepared/uncertain lookup: no successful import or ledger deletion.
- [x] Add subprocess crash before/after import commit; transaction leaves all original admissions or all exact imported records, never a partial legacy/recovery settlement.
- [x] Run `PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -p 'test_runner_result_import.py' -v`; observe failures.
- [x] Implement bounded evidence lookup before held/unknown-effect early returns in reconciliation. Reuse the same import transaction for normal and recovered successful responses. No controller callback or current-focus side effects on import.
- [x] Run import/dispatch tests and existing action/research/session recovery suites; assert legacy uncertain operations without mappings remain held.
- [ ] Commit: `feat: import runner results without executing tool callbacks`.

## Task 6: Exact backend confirmation of a receipt-backed held tool

**Files:** Modify `letta_recovery/{contracts,wire,state-machine,execution,protocol}.mjs`, `prototype/andrewbot/recovery.py`; create `letta_recovery/tests/runner-result.test.mjs`; create `scripts/tests/test_runner_backend_protocol.py`, reusing the existing real-backend protocol setup. Receipt proof also extends `runner_recovery.py` with the full immutable identity.

**Interfaces:**
- Command `recovery_reconcile_tool` carries existing binding/scope/claim/fence/expected revision plus `effect_admission_id`; no response bytes or prose.
- `RecoveryExecution.reconcileTool(binding, claim, admissionId)` fetches exact scoped controller proof; transition `runner_tool_confirm` accepts only held/tool_uncertain/tool_admitted with a matching allowed runner tool and valid imported receipt metadata.
- Advertise `runner_result_recovery_v1: 1` and `held_tool_result_v1: 1` only in the new coordinated artifact.
- `LocalRecoveryClient.reconcile_tool(binding, scope, claim, admission_id, expected_revision)`; coordinator imports first, verifies normal continuation guards, acquires fresh claim, reconciles then resumes the same input/operation.

- [x] Write Node tests: exact proof transitions only that tool to confirmed/tool_confirmed; stale fence/revision, other binding/admission/digest, wrong receipt/store, browser tool and provider/storage/owner holds refuse mutation.
- [x] Test idempotent repeat against identical already-confirmed result; changed proof fails. Fault before/after checkpoint replacement reopens at held or confirmed with the same input/tool IDs.
- [x] Add real-backend integration: fake worker executes once, controller loses result, receiver import plus held confirmation completes one continuation; every previously confirmed step is reused. Add owner input/pause during import: no continuation after guards change.
- [x] Run `node --test letta_recovery/tests/runner-result.test.mjs`; observe failures; then implement command validation, gated proof fetch and transition.
- [x] Build an isolated release with `scripts/build-letta-recovery.py` and run the real-backend integration with `ANDREW_LOCAL_RECOVERY_TEST_RELEASE` pointing only at that artifact. No npm/live runtime modification.
- [x] Run all Node recovery tests plus owner-handoff/backoff/crash protocol regressions. Verify no prepared/unknown receipt can take this transition.
- [ ] Commit: `feat: confirm held runner tools from exact imported receipts`.

## Task 7: Three-store snapshots and restore barriers

**Files:** Modify `memory_backup.py`, `memory_backup_cli.py`, `runtime_readiness.py`; extend `prototype/tests/test_recovery_backup.py`; create `prototype/tests/test_runner_receipt_restore.py`.

**Interfaces:**
- `create_runner_recovery_snapshot(controller_path, backend_home, runner_root, backup_dir, *, workers_quiescent: bool) -> dict` builds versioned `runner_recovery_snapshot_v1` with all store IDs/source digests/key fingerprint and no plaintext key.
- `verify_runner_recovery_snapshot(directory) -> dict` verifies all three stores, immutable identities and encrypted result hashes.
- `restore_runner_recovery_snapshot(snapshot, controller_path, backend_home, runner_root, *, key: bytes, workers_quiescent: bool) -> None` preserves all current stores as pre-restore evidence and fences dispatch before replacement.
- Keep existing paired snapshot APIs compatible. Controller memory restore preserves `runner_operations`/`runner_imports`, original attempt linkage and newer outcome evidence; conflicting identity/result histories abort.
- Readiness gains administrator-owned `/etc/andrew/runner-receipt-adoption.json` bound into runtime proof; older feature-unaware execution cannot start on adopted stores. No fence is cleared automatically.

- [x] Write restored-older-controller/receiver tests: newer mapping/admission/result survives, same operation cannot execute again, original research/owner/replay barriers remain.
- [x] Test changed runner/controller/store UUID, missing/wrong key fingerprint, divergent result/ciphertext/identity and truncated snapshot: refusal retains all pre-restore evidence.
- [x] Test active worker/socket owner or non-quiescent process prevents snapshot; restore process loss at each replacement phase leaves fences on. Metadata contains no request/output/key/grant sentinels.
- [x] Run `PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -p 'test_runner_receipt_restore.py' -v`; observe failures.
- [x] Implement snapshot/restore and newer-ledger preservation using immutable operation identities, and adoption verification in readiness. Never infer success from worker cleanup.
- [x] Run new and existing memory/recovery-backup/readiness tests; verify old paired snapshots remain readable with receipt execution disabled and cannot authorize adopted execution.
- [ ] Commit: `feat: preserve runner receipts across coordinated backup and restore`.

## Task 8: Coordinated candidate, credential and deployment acceptance

**Files:** Create installer/credential scripts from file map, `scripts/runner-recovery-manifest.json`, `scripts/tests/test_runner_recovery_installer.py`, `scripts/tests/test_runner_receipt_crash_protocol.py`; modify `systemd/andrew-runner.service`, `scripts/install-p1-fixes.py`, `scripts/install-letta-recovery.py`, `scripts/tests/test_credential_setup.py`, `scripts/diagnose-session.py`; add `docs/runner-result-recovery-operations.md`; update `Memory update.md`.

**Interfaces:**
- Credential script provisions protected encrypted `andrew-runner-receipts-key` exactly once, refuses replacement if identity/ledger exists, and prints only safe status/fingerprint. Runner loads it from systemd `CREDENTIALS_DIRECTORY` with `LoadCredentialEncrypted`; no environment key bytes.
- Installer functions `build_candidate(backend_release, controller_sources, runner_sources, directory) -> dict`, `verify_candidate(candidate, *, require_acceptance: bool) -> dict`, `activate(candidate) -> dict`, `enable(candidate) -> dict` bind both source trees, backend and exact tests.
- CLI uses `--backend-release PATH --apply`, separate `--enable EVIDENCE_DIR`, and inspection/preview modes. Initial activation disables runner-receipt execution in both runner/controller. Enable pins explicit receiver/controller identities and capabilities while preserving pause; it does not request wake.
- Stop controller/backend/runner service and runner socket, assert no worker owner, save/verify Task 7 snapshot, then deploy only reviewed source merges. Use `/opt/andrew/runtime-services/andrewbot` as well as controller package; bind new shared dependency modules to the manifest/proof.
- Interrupted activation leaves affected services/socket stopped with phase evidence. Rollback refuses adopted current stores; isolated pre-adoption rollback must retain all later stores outside its tree and keep dispatch fenced.

- [ ] Write fake-service installer tests for unsafe credential replacement, unknown deployed hashes, missing peer capabilities, partial code rollout, busy worker, owner-state change, snapshot failure and crashes after each deployment phase. Every case refuses replay/unsafe fallback startup.
- [ ] Write real separated-process crash matrix for controller death after receiver commit, runner death before/after commit, controller death before/after import and backend death during held confirmation; count one worker execution where a result exists, zero re-executions otherwise.
- [ ] Add diagnostic tests allowing only fixed states/counts/IDs/hashes/cleanup flags, with secret sentinels absent. Add service-user access and mount/credential isolation checks; privileged isolated acceptance must use a root-created listener accepted by the runner UID and verify runtime-sender credentials without launching a worker; fresh source/runtime proof must match both packages.
- [ ] Run `PYTHONPATH=prototype:scripts:scripts/tests python3 -m unittest discover -s scripts/tests -p 'test_runner_recovery_installer.py' -v`; observe failures, then implement the reviewed candidate and disabled-activation flows.
- [ ] Full acceptance runs the exact merged candidate: all Node tests, `python3 -m unittest discover -s prototype/tests -q`, `python3 -m unittest discover -s scripts/tests -q`, patched real-backend protocol and pristine compatibility suites. Capture commands, candidate/source hashes, named fault-case results, skips and execution counts in its acceptance artifact.
- [ ] Fresh independent whole-patch review plus self-check against every approved-spec requirement; fix material findings and repeat only affected checks/full acceptance when runtime inputs change.
- [ ] Produce a concrete reviewable artifact and installation instructions. Leave production unchanged until deployment is authorized; preserve pause, then verify explicit wake, a delivered owner reply and a completed autonomous turn before closing this increment.
- [ ] Commit: `feat: package coordinated runner result recovery rollout`.

## Plan self-review and handoff

Spec coverage: Task 1 owns shared identity/format; Task 2 encrypted durability/capacity; Task 3 private authentication/handshake/ownership; Task 4 admission and owner cancellation; Task 5 historical import/provenance/research; Task 6 exact held-step confirmation; Task 7 three-store restore; Task 8 coordinated artifact/credential/readiness/acceptance. All five review-focus conditions have named tests in their owning tasks.

Use an isolated checkout at execution time and preserve the current uncommitted handoff/deployment work. Each task's commit must include only its own completed files; do not include unrelated existing edits. Runtime implementation remains in the isolated checkout; no production mutation has been performed by this plan. Native sequential execution is recommended; implementation is proceeding sequentially in the isolated checkout; remaining integrations and coordinated acceptance are still required.
