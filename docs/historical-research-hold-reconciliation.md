# Historical research.save rejection reconciliation

This is maintenance tooling for P0-0, the exact admitted new-entry call whose
findings have 2,116 characters against the historical 2,000-character limit. The
request was rejected before journal storage. P0-0b prevents future occurrences;
it does not retroactively settle this admitted call.

The live inspection identifies owner revision 38 in the old binding and revision
39 with an explicit pause now. The old cycle must be retired rather than resumed.
The inspected database has two research entries, seventeen held messages and ten
legacy reservations. None may be changed or replayed by this repair.

## Proof and scope

`scripts/inspect_research_hold.py` is read-only. It validates the exact effect,
scope, request and argument hashes against the released tool_uncertain backend
checkpoint. Every prior effect in the binding must have matching confirmed
receipts and provider usage must already be settled. The target has no receipt,
reservation or runner mapping. No other effect may be admitted. The pure research
validator must produce the exact findings-limit rejection without a store callback.
Only hashes, counts, opaque identities and fixed validation metadata are output.

The apply command requires an explicit SHA-256 of this inspection proof and the
fixed production effect admission ID. Before stopping services it verifies the
installed P0-0b package, adopted store identities, unchanged backend and a fresh
identical inspection. Drift refuses the operation. Owner pause, lifecycle,
revision, restore guards and the protected record hashes are checked again during
the repair and after finalization.

## Offline migration and acknowledgement

The current backend has receipt-only reconciliation for runner results and a
different, narrowly specified diary case. It cannot import this research receipt
through either command. This repair is an explicit offline data migration; it
does not add or weaken a runtime recovery rule and requires no backend release.

The root coordinator stops controller/backend/runner writers, verifies disposable
workers are gone, checkpoints SQLite, and saves verified paired and three-store
snapshots. It holds the backend execution locks while changing the two stores.

Under the controller UID, one transaction records the exact research error and a
deterministic owner-retirement receipt. The admission remains bound to its exact
request. Unknown effects and every other result receipt remain unchanged.

Under the backend UID, the offline migration loads the exact original checkpoint,
verifies its identity and payload hashes, imports that one error result and uses
the existing owner_yield transition to produce a paused terminal. The result blob
is written durably before the atomic checkpoint replacement. Previous provider
and tool steps are preserved. Repetition imports the same receipt, rather than
executing any tool or model callback.

Finalization starts only the backend plus a temporary controller-UID gateway. The
gateway loads only the existing internal systemd credential; it has no provider
keys, blocks provider completion and tool admission/execution, and exposes only
evidence/result/settlement routes. Existing protocol commands project the saved
error once, acknowledge settlement, release the claim and retire the old scope.
An interrupted projection or acknowledgement can be repeated without dispatch.
The newer pause and local_recovery_interrupted error remain in place.

Finally, protected table hashes and the provider usage ledger are compared with
the original proof/snapshot, normal services are restarted, and a sanitized result
is written to `/home/daniel/andrew-research-hold-result.json`.

## Failure and resume

The administrator-owned maintenance journal records the exact proof and snapshots.
Failures after stopping writers retain a startup fence and keep services stopped.
An additional temporary controller boot condition remains active while the backend
runs for projection, so a power failure during that phase cannot start the normal
controller before maintenance finishes. Both guards are removed after verification.
The same command can resume a valid recorded receipt/checkpoint/finalization phase.
Incomplete snapshots or changed proof/data refuse; no automatic rollback discards
new receipts. A failed or timed-out transient gateway is stopped as well.

No owner pause is cleared, no historical provider request or tool is repeated,
no reservation is released, and no held owner message is requeued. A subsequent
owner wake is a separate action.

## Validation

Focused tests cover read-only proof and content privacy, mismatched owner/restore/
checkpoint/request/effect evidence, refusing drift before service changes, fencing
after a receipt-stage failure, exact resume, and transient-service timeout cleanup.
The actual pinned backend tests cover interrupted receipt/checkpoint/projection
phases, repeated repair, preservation of existing research/messages/reservations,
one projected rejection, no additional provider dispatch, and a fresh input after
an explicit synthetic owner wake. No broad suite rerun is required for these
maintenance-only files; the deployed runtime packages are unchanged.
