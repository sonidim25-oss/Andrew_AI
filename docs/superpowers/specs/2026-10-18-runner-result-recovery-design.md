# Durable recovery of completed runner results

**Date:** 2026-10-18  
**Status:** Written design approved by the owner on 2026-10-19; implementation plan prepared for review.  
**Scope:** Newly journaled autonomous `python.run`, `node.run`, and `shell.run`.

## Outcome

Recover an exact successful runner response after the controller loses its reply
or crashes before saving the tool result. Fetch existing evidence without
executing the command again. Preserve the original model input, tool admission,
cycle linkage, owner controls, and untrusted provenance.

The owner approved this increment after the
[source investigation](../../runner-result-recovery-investigation.md). Andrew
continues running during design and offline implementation. A subsequent
installation requires a paused, idle runtime and coordinated service shutdown.

A successful tool response proves that the invocation returned that response.
It does not independently prove a publication, recipient delivery, or another
claim made by arbitrary program output. Browser/account actions, media, billing,
legacy attempts, owner-message turns, and destination-specific verification are
outside this increment. Missing evidence never authorizes a retry.

## Architecture

Keep the existing isolated runner and controller. Add a versioned protocol and
receiver ledger to their private Unix socket; add no public listener or worker
mount. Recovery metadata stays outside the worker's tool request and model tool
arguments. The browser process and its protocol remain separate.

| Component | Responsibility |
| --- | --- |
| Controller runner-admission adapter | Bind the runner operation to an existing recovery effect and legacy cycle attempt before dispatch. |
| Runner receipt store | Durably admit one execution and save its validated response before replying. |
| Private runner protocol | Authenticate peers, negotiate capabilities, authorize dispatch, and provide execution-free lookup. |
| Controller evidence importer | Validate saved receiver evidence and atomically settle the exact local tool records without callbacks. |
| Backend receipt reconciliation | Advance only the exact admitted tool using confirmed controller evidence and current fenced ownership. |

Use repository-owned modules shared by the controller and separately deployed
runner service. Bind both distributions and the backend extension to their exact
source hashes in the deployment artifact. Enabling this feature requires all
three components to advertise the new capabilities. A missing capability must
hold affected work; it must not trigger an unjournaled fallback.

## Identities and controller admission

Define `runner_result_recovery_v1`. Each operation has:

- A persistent runner-store UUID and controller-store UUID.
- The complete existing recovery binding and its canonical SHA-256 digest.
- Existing tool admission ID, effect ID, request digest, and tool name.
- A random 256-bit operation ID, unique in both stores.
- The exact legacy `cycle_tool_attempts.attempt_id` and saved research context.
- A worker-request SHA-256 digest and formatter version `runner_text_v1`.

Canonical hashes use the existing recovery canonicalization. The tool digest
covers `{name, arguments}`; the worker digest separately covers the validated
`{type, code}` or `{type, command}`. The controller stores the mapping and verifies
both. Do not persist raw code in the new receiver ledger. Existing backend
argument payloads remain the source needed for identity checks.

For an eligible tool, one controller transaction records its immutable runner
mapping, legacy cycle attempt, research context and untrusted provenance while
the recovery effect remains `prepared`. It commits before any runner RPC.
Mapping preparation is not execution admission. After `ready`, the controller
revalidates authority under its store lock, calls the normal `begin_tool` gate,
and atomically records the mapping as dispatch-admitted before sending the grant.
A crash after that commit is conservatively uncertain until receiver evidence is
available. No database transaction stays open across socket I/O.

The adapter owns this path instead of invoking the existing recursive callback;
that callback must not create another legacy attempt or run a second command.
An owner stop before dispatch admission uses the existing prepared-effect
cancellation proof and marks the linked legacy attempt `not_executed` in the
same transaction. This outcome records cancellation, not successful execution.
It must not be counted by queries for `started`/`uncertain` tool attempts. Persist
the operation identity before dispatch so a lost reply cannot create a new ID.

The controller pins the runner-store UUID during explicit initial provisioning.
A different UUID at startup or lookup is a mismatch, not an empty new ledger.
The new controller mapping table has unique keys for operation ID, recovery
admission, and legacy attempt ID; immutable identity columns cannot be updated.

## Private protocol and authorization

Retain the current 0600 `andrew-control` Unix socket. The runner verifies Linux
`SO_PEERCRED` on the accepted connection: only the configured controller UID may
submit private recovery requests. The controller verifies the actual sending
runner UID using `SO_PASSCRED`/`SCM_CREDENTIALS` on received frames, rejecting
missing/truncated credentials or a different sender. Do not use the client's
`SO_PEERCRED` value alone to identify a systemd-activated runner: listener-time
credentials can belong to the service manager. This follows the documented
[Linux Unix-socket credential semantics](https://man7.org/linux/man-pages/man7/unix.7.html).
Validate actual distinct listener/service UIDs in privileged isolated acceptance. No new shared
gateway token or mailbox/provider credential is given to the runner. Tests inject
expected UIDs explicitly; production resolves named service users.

Three messages are defined:

1. `capabilities`: reports contract/version, runner-store UUID, formatter
   version, receipt capacity status, and whether execution is enabled.
2. `execute`: carries the immutable operation identity and bounded worker
   request. A new operation uses the dispatch handshake below. An identical
   previously admitted operation never launches another worker.
3. `lookup`: carries the complete expected immutable operation identity and no
   executable payload. It returns `missing`, `prepared`, `admitted`, `uncertain`, or `result`
   with exact bounded evidence. This path cannot call `RunnerService.run`, clean
   containers, allocate a new operation, or enter a dispatch handshake.

Worker payloads retain the existing 64 KiB serialized request limit and strict
field validation. Bound the new envelope to 80 KiB and each metadata document to
8 KiB; responses allow the existing 40 KiB worker response plus 8 KiB metadata.
Legacy frames keep their existing limits and behavior. Unknown fields, oversized
frames, extra trailing frames, and unsupported versions fail closed.

### Dispatch handshake

The runner validates identity, pins the already provisioned controller identity,
checks capacity, persists a new `prepared` record, and replies `ready` with a
fresh random challenge and the exact operation digest. It has not started a
worker. The live controller revalidates the existing recovery scope, owner
revision, pause/sleep, pending owner input, budget permission, boot ID, and backend
lease, then sends a challenge-bound `dispatch` grant on the same connection.

The grant includes the original scope/fence and lease deadline, using the
existing monotonic clock and boot identity. The runner rejects expired grants,
foreign boot IDs, changed operation digests, reused challenges, or a closed
connection. It atomically changes `prepared` to `admitted` for one accepted grant while
holding the execution-owner lock, commits that change, and only then starts the
worker. No separate reconnect can dispatch an admitted operation.

An owner stop after the grant can race a command already admitted to execute;
preserve the existing peer-disconnect cancellation and cleanup behavior. This
handshake narrows authorization to the live controller immediately before
dispatch; it does not promise instantaneous rollback of an accepted command.
Lookup never renews or replaces a dispatch grant.

## Receiver persistence

Store `identity.json`, a process-owner lock, and `receipts.sqlite3` under
`/var/lib/andrew/runtime/runner-receipts`, owned by `andrew-runtime`, directory
mode 0700 and files 0600. Open without following symlinks; reject unsafe file
types/ownership. The directory is outside `/workspace` and is never mounted into
the container. Acquire an exclusive kernel lock before loading the ledger or
recovering containers. SQLite uses `synchronous=FULL` and DELETE journaling;
commit and parent-directory durability follow the repository's existing helpers.

Provision a dedicated 32-byte `andrew-runner-receipts-key` using the existing
systemd encrypted-credential pattern. Load it only into the runner service.
Encrypt saved response bytes with AES-GCM, using a fresh 96-bit nonce and the
contract plus immutable operation identity as associated data. The controller
receives the decrypted response only through the authenticated socket. Keys,
grants, raw code, stdout/stderr, and responses must not enter diagnostics or logs.
Provisioning refuses key replacement if any ledger/identity already exists;
missing or invalid credentials with existing data prevent startup. Receipt
encryption must use an explicit credential file, not an environment key value.

The ledger retains immutable identity, admission/completion times, state,
response digest, ciphertext, formatter version and verified cleanup status.
`prepared` means no grant has been accepted; it is not evidence of execution. A
lookup of a live prepared operation does not itself fence a grant that could
still arrive. `admitted` means execution may have happened; `uncertain` means no importable successful result is available;
`result` means a complete validated response was durably stored. It records all
bounded responses, but only `status: ok` with `safe_error: null` is eligible for
automatic successful reconciliation, and only when managed-worker cleanup was
verified. An error response or failed/unverified cleanup stays uncertain about
possible external effects. A successful-looking response cannot override an
unhealthy runner or failed container removal.

Persist the normalized, bounded worker response before sending any final reply.
Use one shared strict validator and one pure versioned formatter for normal and
recovered paths. Store the exact bytes and their digest. An invalid, truncated
transport frame or oversized response cannot become a success receipt. Preserve
the existing output content limits; no inference from partial stdout.

Keep the executor occupied until cleanup and receipt persistence finish. The
executor future callback must not clear `_busy`; the socket handler releases it
only after both obligations finish. Slow/failed receipt storage must not admit
another execution. Lookup remains independent of the execution owner.
Disconnect cancellation remains active. If a valid final response is available
when the peer disconnects, persist it before returning; if cancellation or
cleanup leaves the result ambiguous, retain uncertainty. A save failure after
execution must not return an unpersisted success or permit re-execution.

On service restart, validate the ledger, acquire its owner lock, and clean
interrupted managed workers using the existing runner cleanup. Rows with an accepted grant but without a saved result remain uncertain.
Pre-grant rows retain their distinct `prepared` identity and cannot be submitted
again to start execution. They are not eligible for successful result import. Container disappearance or cleanup success does
not establish the command's external outcome. Saved results stay readable.

### Capacity and retention

Default receipt capacity is 256 MiB, with at most 50,000 operation records.
Reserve a 64 KiB slot per new admission in the same transaction; replace its
padding with the bounded encrypted result. Account for database allocation and
metadata, refusing new dispatch before either limit. A filesystem-full commit
still leaves uncertainty; reserved space is not a promise against all I/O faults.
Lookup remains available at capacity. Never evict unresolved records or reuse an
operation identity. This increment has no automatic expiry or prune command;
increasing capacity requires an explicit administrator configuration change.

## Controller evidence import

Before `_check_outcomes` blocks a pending binding, inspect only its existing
runner mappings and perform authenticated `lookup` calls. No callback, arbitrary
command, browser interaction, or model request is allowed on this path. Missing
or conflicting evidence leaves the existing hold intact. Bound lookups to one
pending operation per worker pass with a five-second socket deadline.

Validate contract, pinned store UUIDs, every binding/link/digest, formatter
version, complete response schema, successful status, verified cleanup status, and response digest. Apply
the same pure formatter to produce the exact normal model-facing tool result,
including its untrusted-data treatment. Receipt artifact paths remain references;
their existence or unchanged contents are not proved by a saved response.

One controller transaction then:

1. Inserts immutable imported evidence keyed by the runner operation ID.
2. Saves the normal `recovery_results` entry and confirms that exact admitted
   recovery effect, refusing any existing divergent result.
3. Marks its linked legacy cycle attempt returned and records the saved research
   observation idempotently, keyed by attempt ID rather than current focus.
4. Preserves the binding's untrusted flag and marks the mapping imported.

Repeated import verifies identical evidence and is a no-op. A crash before the
transaction leaves the original admission; a crash afterward leaves confirmed
records. Never invoke the tool wrapper to reconstruct bookkeeping. Changes to
research focus/revision cannot cause the result to attach to a different entry.

Evidence import establishes historical completion, not permission for more work.
It may retain proof after owner pause or revision change, but it cannot clear
those controls, activate a session, or launch another step. Resumption still
requires the original valid binding, no pending owner input, no restore fence,
and no unrelated uncertain tool, provider reservation, delivery, or media action.

## Backend checkpoint reconciliation

Advertise `runner_result_recovery_v1` and `held_tool_result_v1` as additional
capabilities under the existing local durable-recovery contract. Require the
coordinated backend version before enabling runner receipt execution.

For `tool_admitted`, imported controller proof is consumed through the existing
result-confirmation path. For a checkpoint already held, add one private
`recovery_reconcile_tool` command and corresponding transition. It requires:

- `phase == held`, `hold_reason == tool_uncertain`, and
  `held_phase == tool_admitted`.
- The original binding, current expected revision, and a fresh valid fenced
  claim admitted under the existing owner guards.
- Exactly the matching admitted tool/effect/admission/request digest and an
  imported runner receipt of an allowed tool type.
- Confirmed controller evidence fetched through the existing scoped gate;
  caller-supplied prose or response bytes cannot establish proof.

Commit the normal result payload, mark that tool confirmed, clear only that hold
reason/held phase, and move to `tool_confirmed`. Other steps and operation IDs
are unchanged. A repeated command must either match an already confirmed result
exactly or fail without mutation. Unknown tools, provider holds, storage errors,
configuration/owner mismatches, and other hold categories cannot use this path.

The coordinator may consider a previously held attempt only when its exact
runner receipt qualifies and all continuation guards pass. Import first, check
unrelated uncertainty, acquire a new authorized scope/claim, reconcile the exact
checkpoint, then resume normal execution. Every already confirmed tool is reused,
including the recovered result; no tool request is resubmitted to the runner.

## Backup, restore, and deployment

Extend administrator recovery snapshots to include the controller, backend, and
runner receipt store as a single versioned set. Stop the controller/backend,
runner service, and runner socket before the snapshot; verify no receiver worker
or execution lock remains active. A busy/cancelled worker must not be called
complete merely to pass installation. Snapshot digests bind all three store
identities and preserve the encrypted runner responses. The runner credential
stays in its existing root-managed encrypted credential store, not plaintext in
the snapshot. Record its identity fingerprint so the wrong key is rejected.

Restore retains all three current stores as pre-restore evidence, sets dispatch
barriers before replacement, and preserves newer runner admissions/results by
immutable operation identity. Conflicting evidence aborts restore. Controller-only
memory restore also preserves runner mapping/import tables with the other newer
ledgers and leaves restore fencing in place. An older controller snapshot cannot
erase an external execution or authorize a new operation for the same admission.
No restore path clears these fences automatically.

The installer must validate a fresh candidate against deployed controller and
runner sources, run offline acceptance, provision/check the dedicated credential,
verify service-user permissions, and save the three-store snapshot before changing
code. Keep feature execution disabled during initial activation; check read-only
capabilities, pinned identities, and source/runtime proof before separate enable.
Preserve owner pause and require an explicit owner wake afterward.

On installation failure after shutdown, leave affected services stopped and
retain phase evidence. Do not replay a command to repair installation. An older
binary must not start on a receipt-adopted store: add an administrator-controlled
adoption check to runtime readiness and rollback verification. Returning to an
older runtime requires an isolated pre-adoption snapshot and reconciliation of
all post-snapshot operations, preserving the current evidence outside that tree.

## Acceptance

Use local fake workers and destinations, real sockets, terminated subprocesses,
and reopened SQLite stores. No live publication, paid inference, or actual
provider call is required.

| Boundary or fault | Required result |
| --- | --- |
| Controller dies after receiver result commit | Exact result imported; worker execution count remains one. |
| Runner dies before result commit | Operation remains uncertain; no automatic retry. |
| Runner dies after result commit | Lookup returns the same result after restart. |
| Controller dies after receipt import | Reimport is identical; legacy attempt/research observation occurs once. |
| Backend holds before evidence arrives | Only the exact receipt-backed tool hold can transition to confirmed. |
| Backend dies during that transition | Reopen yields the previous hold or the complete confirmed result. |
| Owner input or pause before controller dispatch admission | No worker starts; the prepared-effect cancellation path settles only that mapping and legacy attempt as not executed. |
| Peer disconnects before runner accepts dispatch grant | No worker starts; the prepared identity cannot be reused to execute or imported as a successful result. |
| Peer disconnects/cancels after dispatch | Cancellation/cleanup retain exclusive ownership; partial effects remain unknown. |
| Duplicate ID or changed request/store identity | No second worker; mismatch rejected without overwriting evidence. |
| Pause, sleep, owner revision/input, expired lease | No new dispatch/continuation; historical evidence cannot override control. |
| Lookup at capacity or while busy | Read-only evidence remains accessible; execution count unchanged. |
| Worker returns while receipt storage is blocked | A second execute remains rejected until cleanup and durable storage finish. |
| Worker reports success but container cleanup fails | No importable successful receipt; runner stays unavailable for new execution. |
| Owner message or pause races a completed-result import | Historical result is retained without clearing owner controls or dispatching another step. |
| Malformed response, wrong credential, unsafe file, corruption, full disk | No fabricated success; no replay. |
| Older snapshot or interrupted installation | Newer evidence retained; dispatch fenced pending reconciliation. |

Also verify raw request/output/key/grant absence from diagnostics, no receipt
directory/credential mount in workers, authenticated peer rejection, existing
legacy runner and browser compatibility, exact formatter parity, real service-user
file access, unrelated uncertainty blocking, and unchanged owner-message handling.

## Explicit limits of this increment

This increment recovers exact completed successful responses. It does not promise
exactly-once effects from arbitrary programs or complete recovery from every
failed handshake. A grant admitted by the controller followed by a lost socket,
with no terminal receiver result, remains held. A `prepared` receiver lookup on
its own is not a durable proof that no future grant can be accepted. It must
never be promoted to a successful response, used to delete a reservation, or
used to authorize another execution. General negative-evidence reconciliation
requires a separate permanently fenced cancellation receipt and is not inferred
from absence of a result.

The legacy uncertain Python invocation in the live incident predates this
protocol and remains uncertain. Installing these receipts cannot certify it
retroactively. Owner-message handoff, pause, local-control routing and replay
exclusions from the installed repair remain required regression cases.

## Implementation and rollout checkpoints

1. Shared protocol validation, formatter and encrypted receipt-store tests run
   against fake workers, without wiring new execution into Andrew.
2. Isolated real-socket runner/client tests exercise grant, disconnect, cleanup,
   capacity and process-loss boundaries; legacy protocol remains compatible.
3. Controller import and exact backend confirmation tests prove one execution,
   idempotent ledger/research bookkeeping, and owner-control preservation.
4. A candidate binds the runner, controller and backend sources and passes the
   failure matrix before production execution can be enabled. Installation uses
   a coordinated three-store snapshot and preserves owner pause.

The feature stays disabled through the first three checkpoints. Each checkpoint
requires evidence for its behavior, rather than a passing aggregate count alone.

## Review and next step

The owner approved this revised written design on 2026-10-19. The concrete
[implementation plan](../plans/2026-10-19-runner-result-recovery.md) is ready for
review. Native sequential execution is recommended because the runner, controller
ledger, backend transition and installer share this contract. Runtime
implementation starts after plan review and execution-method confirmation.
No runtime code or production state is changed by these documents.
