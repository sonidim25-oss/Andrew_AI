# Runner receipt implementation checkpoint

The owner directed continuation after the diary repair installation. Implementation
uses `/home/daniel/andrew-runner-receipts`, an isolated checkout seeded from the
current deployment checkout. The deployed controller and runner are unchanged.
The runner receipt feature has no production execution or import path yet.

The approved design is in
[runner result recovery design](superpowers/specs/2026-10-18-runner-result-recovery-design.md).
The eight-task plan is in
[runner result recovery plan](superpowers/plans/2026-10-19-runner-result-recovery.md).

## Implemented foundation

Task 1 extracts the existing canonical identity functions without changing their
behavior or import compatibility. It adds deeply immutable operation, grant and
evidence documents, strict field/state checks, bounded JSON frames and pure
request preparation, response validation and versioned formatting. Existing
Python/Node/shell result text retains the exact 15,000-character truncation and
untrusted-output suffix. Browser work is outside the receipt contract.

Task 2 adds an explicitly provisioned receiver store with a persistent UUID,
controller identity and key fingerprint. Files are private, unsafe paths and
changed files are refused, and a kernel lock excludes another process owner.
SQLite uses FULL synchronization and DELETE journaling. AES-GCM encrypts saved
responses with a fresh nonce and authenticates operation identity, cleanup status
and response digest. Code, output and keys are absent from plaintext metadata.

Preparation and execution admission are distinct states. Existing operations
return saved evidence; they cannot be admitted again. Restarted unfinished
admissions become uncertain. An error response or failed cleanup cannot become
importable success. Capacity includes database allocation, metadata, journal
headroom and 64 KiB per unresolved operation. Receipts cannot be deleted or have
their identity changed through the ledger schema. Lookup remains available at
capacity and uses a separate read-only connection, exposing only committed
evidence while the result writer is blocked.

## Evidence

There are 14 new identity/format tests and 17 receipt store tests. Receipt cases
use real reopened stores and subprocess termination on both sides of admission
and result commits. They cover failed commits, altered identities/ciphertext,
cleanup changes, wrong credentials/UID, unsafe paths, file replacement, retention,
capacity and a blocked writer with concurrent lookup/capabilities.

Tasks 1–2 controller regression passed 593 tests (two live skips); all 77 backend
tests passed. The tested source hashes remained unchanged. Full regression
evidence and exact source hashes are retained in
`.superpowers/sdd/2026-10-19-runner-result-recovery/` in the deployment checkout.
The task-only patch is `task1-2.patch`. It applies against the captured current
source baseline; it is a review artifact, not an installable candidate.

## Authenticated socket handshake

Task 3 adds `runner_socket.py` and `runner_receipt_server.py` in the isolated
checkout. Client response chunks use SCM_CREDENTIALS rather than listener-time
SO_PEERCRED. Unknown or truncated ancillary data and passed file descriptors are
rejected; received descriptors are closed. A child process with an inherited
listener proves actual-sender attribution. A privileged check with distinct
listener/service UIDs remains part of Task 8.

Server requests authenticate the controller's accepted-socket UID. New execution
uses prepared/ready followed by a connection-bound challenge and live same-boot
grant. Expiry is checked again after admission persistence. Existing operation
submission only returns evidence. Ownership covers worker cleanup and result
commit; disconnects and handler cancellation do not release it early. Lookup and
capabilities remain available while a save is blocked. Original legacy frame
sizes, browser routing and disabled-feature behavior are retained. Response
framing rejects extra frames; lookup deadlines are total, and unlimited worker
waits do not inherit the connection timeout.

All 74 runner-family cases passed, including 27 new socket/credential cases.
Final full controller regression passed 620 tests (two live skips); runtime
and runner-test hashes remained unchanged. Combined review artifact:
`task1-3.patch`, SHA
`bc6cf29540eee6630e16ec2b3f6a30d2454459c94c2a482141e0feeca5cc99a0`.
It passes `git apply --check` against the captured deployment source baseline;
`task3-implementation-evidence.json` records exact hashes and logs.

## Controller admission and result import

Tasks 4–5 are implemented in the isolated checkout. Immutable mappings bind the
existing recovery effect to one legacy attempt, persistent store IDs and saved
research context before any runner RPC. The ready/grant boundary checks current
owner controls, pending owner input, scope, lease, store pins and normal outcome
and reservation guards. Recovery tool admission and mapping admission commit in
one transaction; database transactions never span socket I/O.

A narrow prepared cancellation records not_executed for only the exact linked
legacy attempt and mapping, and fences its still-prepared effect. An admitted
operation remains held if its reply is lost. Legacy schema upgrade preserves
foreign keys; pre-existing uncertainty survives the upgrade. Owner-yield proof
accepts only exact runner pre-grant cancellation, and rolls back all cancellation
if a different admitted tool is uncertain.

Normal and recovered successful responses use one atomic importer. It validates
pinned stores, immutable identity, complete response, cleanup, response digest and
formatter, then records the result, evidence, legacy settlement and original
research observation together. Repeated import verifies identical evidence and
has no callback or research side effect. Real subprocess exits around commit
prove there are no partial settlements. Proof reads also verify saved research
event linkage and current store pins. Output remains untrusted.

Historical import leaves owner controls, restore fences, unrelated reservations,
legacy unknown attempts and held messages unchanged. Coordinator reconciliation
looks up at most one existing operation before its held-scope early return. It
can import while paused but does not contact the backend or start inference on
that import pass. The next execution still requires normal authority.

Focused evidence: 23 dispatch, 11 import, 2 migration and 3 real-socket controller
cases passed. Final source-pinned controller regression passed 659 tests (two
live skips), and 19 existing real-backend protocol cases passed. All tested
source hashes remained unchanged. Combined review artifact: `task1-5.patch`,
SHA `8c1248ddcd30c585db8fdd216056d8859796e73e5e85e35d9c9b7ffa30e15b43`;
`git apply --check` passed. `task4-5-implementation-evidence.json` records the
exact tested hashes and logs.
The live environment flag/adoption wiring is intentionally deferred to coordinated
rollout; the feature is disabled in production.

## Exact held runner confirmation implemented offline — 2026-10-19

Task 6 is implemented and tested in `/home/daniel/andrew-runner-receipts`.
The backend now accepts `recovery_reconcile_tool` with binding, fresh claim,
fence, scope, expected revision and effect admission ID. The command carries no
result bytes. It fetches the exact scoped controller proof and cross-checks the
full immutable receipt identity, binding, original tool arguments/request,
worker request, operation/store IDs, formatter, cleanup and result digest.
Only the matching held runner tool can become confirmed. Other holds and tools
remain unchanged. Normal successful receipt-backed tool confirmation also saves
the marker, so a lost acknowledgement can be checked idempotently.

The coordinator imports historical evidence first, checks normal owner/outcome
and continuation guards before fresh authority, confirms on a separate pass,
then resumes the same run/input through the normal guarded path. No import or
confirmation pass invokes a worker or provider. Paused, sleeping, pending owner
input, changed owner revision and restore fences prevent continuation authority.

Validation passed: 664 controller tests (2 live skips), all 84 Node tests,
4 new real-backend receipt cases, 19 existing real-backend protocol cases and
7 existing process-crash regression tests. New cases prove a lost runner reply
survives backend restart, exactly one fake worker execution feeds exactly one
continuation, normal receipt confirmation works, owner pause blocks resume, and
the coordinator completes import/confirmation/continuation on separate passes.
Separate process exits before/after checkpoint replacement retain the original
run/input/tool identity and do not repeat execution. Tested source hashes were
pinned and verified unchanged.

Combined 32-file review patch: `.superpowers/sdd/2026-10-19-runner-result-recovery/task1-6.patch`,
SHA `5bf68b387fc66f098032c49c24647e8da9d59a4d2124bb7932cdf7ff4315044f`; `git apply --check` passed against the deployment source baseline.
Exact source/release hashes and logs are in `task6-implementation-evidence.json`.
The isolated backend build is an integration artifact, not an installable rollout.
No production deployment, enable, wake, replay or task commit occurred. Task 7
three-store restore and Task 8 coordinated adoption/credential/rollout acceptance
remain. This checkpoint does not close P0.

## Remaining integration

Task 7 must preserve all three stores and newer receipts across restore.
Task 8 must bind the environment flag, credentials, both service packages,
capabilities and readiness/adoption proof into an initially disabled deployment.
Privileged distinct-listener/service-UID acceptance also remains in Task 8.

The existing uncertain Python attempts and held messages remain outside this new
contract. Current diary deployment enable/live verification remains separately
pending. This work does not close P0 or authorize replay.

## Runner restore and coordinated packaging: offline acceptance complete — 2026-10-20

Tasks 7–8 are implemented in `/home/daniel/andrew-runner-receipts`. Three-store
restore retains current verified execution histories and newer controller
receipts, linked attempts/research records and held messages. Installation binds
both complete packages, encrypted credential, backend release and startup guards;
initial activation disables execution and preserves owner pause. Four material
review findings plus first-install migration and deployed output-truncation
compatibility were reproduced, fixed and independently reviewed.

Final frozen offline candidate: `/home/daniel/andrew-autonomy-media/.superpowers/sdd/2026-10-19-runner-result-recovery/candidate-task7-8-final`.
Validation passed: 84 Node tests; 684 controller tests
(2 expected live skips); 234 script/integration tests;
54 pristine compatibility cases
(37 expected feature skips); actual runtime package imports.
All acceptance logs, exact source hashes and candidate bindings verified.
Evidence: `task7-8-implementation-evidence.json`; reviewed patch
`task1-8-final.patch`, SHA `7d80efdc6dceaf14862105bc24590bae299c86287d812629cf117eb3a2d139b1`.

Production is unchanged. Root is still required to prepare against the private
installed controller package and run the distinct runtime UID listener probe.
Then installation, enablement and explicit live wake/reply/autonomous completion
remain. Existing uncertain attempts and held messages were not replayed.
P0 remains open; later browser/destination evidence and P2 consolidation remain.

## Actual deployed controller preserved: offline acceptance complete — 2026-10-20

Tasks 7–8 are implemented in `/home/daniel/andrew-runner-receipts`. Three-store
restore retains current verified execution histories and newer controller
receipts, linked attempts/research records and held messages. Installation binds
both complete packages, encrypted credential, backend release and startup guards;
initial activation disables execution and preserves owner pause. Four material
review findings plus first-install migration and deployed output-truncation
compatibility were reproduced, fixed and independently reviewed. Actual copied
controller owner-routing, voice-disable and trusted skill startup fixes were
merged and independently reviewed; 55 focused cases passed.

Final frozen offline candidate: `/home/daniel/andrew-autonomy-media/.superpowers/sdd/2026-10-19-runner-result-recovery/candidate-task7-8-host-merged`.
Validation passed: 84 Node tests; 687 controller tests
(2 expected live skips); 234 script/integration tests;
54 pristine compatibility cases
(37 expected feature skips); actual runtime package imports.
All acceptance logs, exact source hashes and candidate bindings verified.
Evidence: `task7-8-host-implementation-evidence.json`; reviewed patch
`task1-8-host-merged.patch`, SHA `6fa86c4ac8d1c67f72a06541fd6ebf49072df23070a0ba93d3d0e9c9b3fcc140`.

Production is unchanged. Root is still required to prepare against the private
installed controller package and run the distinct runtime UID listener probe.
Then installation, enablement and explicit live wake/reply/autonomous completion
remain. Existing uncertain attempts and held messages were not replayed.
P0 remains open; later browser/destination evidence and P2 consolidation remain.
