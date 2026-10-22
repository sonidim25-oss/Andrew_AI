# P2-10: reconcile budget reservations from exact outcome evidence

The operator tool is `scripts/budget_reservations.py`. It never calls a provider,
dispatches tools, replays messages, clears an autonomy error, or changes owner
controls. Old reservations do not expire. An unresolved outcome stays held.

## Report first

```bash
sudo python3 scripts/budget_reservations.py
```

The default database is `/var/lib/andrew/control/controller.sqlite3`; use
`--database` for an offline copy. Inspection opens an existing SQLite database
with `mode=ro` and `query_only=ON`, in one read transaction. It does not initialize
Store or migrate old databases. Output includes IDs, periods, amounts, attempt
outcomes, owner revision, and per-reservation snapshot digests. Request content,
messages, tokens, scope credentials, and provider responses are not printed.

Classifications:

* `proven_not_reached`: finished, durable not-reached attempts bound to the
  **current reservation instance**. Historical attempts sharing the same request
  hash do not qualify. Pre-upgrade attempts have no instance proof and cannot
  qualify retroactively.
* `unresolved`: no sufficient local proof. Do not release based on age, timeout,
  a matching timestamp, absent provider records, or an old attempt's outcome.
* `journaled_requires_protocol`: a journaled reservation or one linked to a
  recovery effect. Generic ledger repair refuses these; their existing paired
  controller/backend receipt protocol must establish and settle the outcome.
* `unsupported_reservation`: not an exact legacy `provider-<sha256>` identity.

The reported `runtime_guard_installed` means the audit table exists. The apply
command additionally checks the exact deployed Store source; table existence
alone is not proof that the running package honors tombstones.

## Evidence file

Copy the exact reservation ID, snapshot digest, and owner revision from a fresh
report. For a current-instance proven-unsent request, the complete file is:

```json
{
  "contract": "budget_reservation_reconciliation_v1",
  "reservation_id": "provider-<64 lowercase hex characters>",
  "snapshot_sha256": "<digest from the report>",
  "owner_revision": "<revision from the report>",
  "outcome": "not_reached"
}
```

For a confirmed historical charge, set `outcome` to `completed` and add:

```json
"billing": {
  "source": "owner_verified_provider_billing",
  "record_id": "<unique provider generation or billing record ID>",
  "request_identity_sha256": "<reservation ID without provider->",
  "cost_usd": 0.031,
  "input_tokens": 123,
  "output_tokens": 45
}
```

**This billing file is a trusted owner attestation, not automatically authenticated
provider evidence.** Independently verify the billing record, amount, tokens, and
its linkage to this exact reservation before approving the digest. A copied
reservation ID or matching time alone does not establish that linkage. Keep the
underlying private provider evidence separately. If exact linkage is unavailable,
leave the reservation unresolved. No provider key or network call is used by this
tool. Conflicting current-instance not-reached evidence is refused.

Use only these fields; private payloads and extra annotations are rejected.
One provider billing record cannot settle multiple reservations. Costs must be
finite and nonnegative; token counts must be nonnegative SQLite integers.

Calculate the evidence digest with the same strict canonical JSON convention:

```bash
python3 -c 'import json,sys; sys.path.insert(0,"scripts"); from budget_reservations import digest; print(digest(json.load(open(sys.argv[1]))))' evidence.json
```

## Install the runtime guard before applying

Deploy the reviewed Store and gateway together through the existing coordinated
runner-recovery installer and its required privileged acceptance. The two release
manifests pin this change's exact source hashes and the `b8e0a7f` baseline. Do not
modify installed files directly or reuse an older candidate's acceptance.

The new Store creates the audit table and instance-binding sidecars during normal
initialization. It preserves the original budget table shape for existing
journaled code. Successful legacy reservations receive unique instance IDs;
tracked attempts bind to the captured instance before dispatch. No backfill
claims outcomes for older attempts. Normal unsent retries still work.

Pause Andrew with the owner command, then stop the controller, runner, backend,
and browser services. Keep them stopped for inspection, backup, and application.
The apply command checks service state and refuses a mismatched deployed Store.
It does not stop or restart services itself. The underlying Python `reconcile`
function is an offline operator API; callers must enforce the same quiescence.

```bash
sudo python3 scripts/budget_reservations.py \
  --apply /private/path/evidence.json \
  --approve-evidence-sha256 <reviewed digest> \
  --backup /private/path/controller-before-reservation-1.sqlite3
```

The backup path must be new. The command creates a mode-0600 SQLite backup and
checks its integrity before any ledger write. No existing backup is overwritten.
Use a new backup path for each invocation, including an idempotent retry.

Application takes `BEGIN IMMEDIATE`, rechecks the exact reservation snapshot,
pause/revision, active-work fields, and restore fences, then atomically inserts
one usage row, one audit/tombstone row, and removes only the selected reservation.
The charge uses the original reservation period and timestamp rather than moving
historical spending into today's totals. Unsent settlement charges zero. The
audit retains the original reservation, reviewed evidence, usage-row ID, and
settlement time. A failed write rolls all changes back.

The tombstone permanently blocks admission of that same legacy request hash.
It does not authorize another attempt. Repeating identical evidence verifies the
existing accounting row and returns success without another charge; different
evidence, changed usage, or a reappearing reservation is refused. Restore the
database and audit together; never delete tombstones to unblock a call.

After verification, restart the services under the existing owner pause. Do not
wake Andrew or replay held messages as part of billing reconciliation.

## Status and validation

Implemented in the isolated `fix/budget-reservation-reconciliation` branch.
Production reservations have not been read or changed: the current shell cannot
authenticate sudo. This is not a claim that historical holds have been resolved.

Regression tests use real SQLite connections and the real gateway. They cover
read-only inspection, legacy databases, current-instance proof, old unsent proof
followed by an identical untracked timeout, exact billing, historical attribution,
tombstone persistence across restart, no provider dispatch after reconciliation,
owner/restore guards, stale snapshots, conflicting evidence, duplicate billing
records, concurrent settlement, interrupted deletion, damaged audit rows,
exclusive private backups, and refusal while services are active.

Verified locally on 2026-10-22: all 22 new reconciliation tests passed; the full
core suite ran 837 tests successfully (2 skips), and the scripts suite ran 310
successfully (94 skips for optional runtime prerequisites). Independent review
found and verified the reservation-instance correction, then reported no
remaining actionable findings. Privileged coordinated deployment acceptance
has not run and is still required before installation.

## Prepared rollout command

Andrew can remain active during preparation. Before installation, send `/pause`
and wait for current work to settle, then run:

```bash
sudo /home/daniel/deploy-andrew-budget-reconciliation-20261022.sh
```

The launcher pins the deployment commit. It refuses dirty sources or a changed
installed baseline (controller `6cd63b2e…`, runner `3b42b8e1…`, backend
`3f5cfd7a…`). `scripts/budget_deployment.py` checks pause, no active turn/session,
restore fences and snapshot headroom before acceptance. It reuses the exact
837-test core proof only when all validated core inputs and Python match. It runs
the remaining required privileged acceptance, including protocol and distinct-UID
checks, against an isolated candidate before changing services. Valid complete
acceptance is reused on retry; incomplete candidates are retained for inspection.

Before applying, the command rechecks the exact owner state, adoption record,
billing rows and snapshot space. The existing coordinated installer takes its
normal backups and fences, then applies and enables the package. The backend and
runner package remain unchanged. Final checks verify expected package digests,
preserved pause/owner controls and unchanged usage, reservations and reconciliation
audit rows. No settlement, wake or replay is part of this command.

The stable pristine test package and saved core proof are under
`/home/daniel/andrew-budget-preflight-20261022/`. The root-owned installation
candidate will be `/var/backups/andrew-budget-reconciliation-20261022-candidate`.
If installation has already started and stopped partway through, inspect
`installation.json` before retrying; the wrapper refuses to overwrite that state.
