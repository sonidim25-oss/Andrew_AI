# Local diary rejection and destination reconciliation

## Incident

After the late compaction result was recovered and the interrupted autonomous
turn was yielded, the owner used `/wake`. The two new queued owner messages
were processed in a new owner input. Its provider response completed at
2026-10-19 02:41:49 UTC. The model requested `save_diary_entry` at 02:41:50.
An owner conversation has no active autonomous session after this wake. The
diary precondition raises before its write, but durable tool admission had
already happened. No tool response was saved, so the backend held the tool
as uncertain and the owner received generic `turn_failed`.

Reproduction with the real isolated backend produced that exact user-facing
failure. This was neither a new provider timeout nor replay of the old Python
programs. The root read-only report is
`/home/daniel/andrew-diary-recovery-check-latest.json`. It identifies:

- Owner scope `5bb2f4ff78604547b595f4660f31a9c4`, owner revision 36.
- One confirmed provider and one admitted `save_diary_entry`, backend revision
  514, held `tool_uncertain` from `tool_admitted`, no backend claim.
- Tool admission `28d35f07c13f270467ccf82364c32ff2c50b9655d6eea6200d92db315fe68e8f`.
- Request digest `ef2e7e3e2d25722408ff72fa62613033dfea5e1ba60ad30dd3550b8c0d769596`.
- No diary rows for the possible local date 2026-10-18, no new autonomous
  cycle and no diary-creation event. Main database ctime precedes admission;
  no restore-evidence files were found.

The diagnostic is not a quiescent snapshot. These observations motivate the
repair; the administrator must revalidate the full proof after stopping writers.

## Future behavior

The no-autonomous-session precondition now returns a fixed tool error:
`status:error`, `error:diary_requires_autonomous_session`, `executed:false`.
Normal tool confirmation durably saves that actual returned rejection. The
model can then answer the owner instead of losing the entire conversation.
No diary is created. Other exceptions, including failure after a diary commit,
remain uncertain; there is no blanket exception-to-success conversion.

## Existing held call

The historical call has no saved precondition rejection. The repair therefore
does not label its callback unexecuted. It verifies the authoritative local
diary destination and records only:

```json
{"status":"error","error":"diary_not_committed","committed":false,"evidence":"quiescent_local_diary_ledger"}
```

The read-only preflight requires one exact halted owner input, exact binding and
effect identities, one settled provider response, one admitted diary tool, no
other admitted effects, no live claims or work, a bounded interval of up to seven Toronto dates, and
an empty diary destination on every possible execution date. Restore flags alone do not prove continuity:
supported restore replaces the main database while preserving recovery records,
but can remove diary rows. A device/inode/birth-time witness predating admission is
captured before stopping services; the same inode must remain after quiescence.
An older ctime is used only if file birth time is unavailable. A normal WAL
checkpoint may advance ctime and does not itself imply a restore. The supported
restore/replacement path is thereby excluded. Direct unsupported
administrator database edits are outside this continuity argument.

The installer stops controller and backend, checkpoints SQLite WAL, takes a
paired snapshot, and revalidates the exact proof against that snapshot. Only
then does it save the negative destination receipt. The result and evidence
digest are tied to the exact tool admission and request. No callback is invoked.

A private `recovery_reconcile_local_diary` command consumes that exact controller
proof with a fresh evidence-only claim. It supports only the single diary tool
held at its original admission. One checkpoint commit confirms the factual error
result and ends this owner input with terminal `stop_reason:error`, preserving
the provider output. The normal transcript writer records the tool error. The
controller then acknowledges and settles this failed terminal. It is complete
as a durable input, not a successful owner answer or successful diary save.

No inference, tool execution, message replay, owner wake, or uncertainty clearing
is part of this repair. Owner controls/error, the old Python uncertainty, legacy
provider costs and held messages remain preserved. Initial activation keeps
automatic recovery disabled; separate enable validates the finalized evidence.

## Interrupted installation

If the receipt committed but installation/finalization stopped, use:

```bash
sudo python3 scripts/install-letta-recovery.py \
  --resume-local-diary /var/backups/andrew-local-recovery-<same-new-candidate>
```

This loads the same private accepted candidate and retained paired snapshot,
verifies the exact saved receipt, accepts only original or reviewed bytes in
a partially copied package, and completes deployment with recovery disabled.
It does not record the result again. A released or expired maintenance claim
may have increased checkpoint revision/fence; immutable checkpoint contents
still must match the original snapshot. Live claims are refused. Terminal
reconciliation and transcript projection are idempotent if the reply was lost.
No original snapshot is restored, and no tool is resubmitted.

## Validation and deployment

Focused evidence: the real-backend reproduction failed before the precondition
fix and answered after it. Local destination repair tests verify one provider
call through reconciliation, preservation of three uncertain Python attempts
and sixteen held fixture messages, repeated finalization, actual tool error in
the next provider context, and a fresh owner reply. They reject changed binding,
different snapshots, database identity change, occupied diary destinations on
earlier or later possible dates, and intervals beyond seven dates. Fault tests cover both precommit finalization failure
and lost postcommit reply. Four Node tests cover exact/missing/foreign proof and
checkpoint commit process loss. The final candidate is
`candidate-local-diary-dates`, SHA
`bc19fede38d051afa1e32ac4a9b13d498b9ba9eaba3ef43ebafc6b36b8a47cb6`.
Full merged scripts acceptance passed (207 cases, 393 seconds); unchanged Node/core stages passed
(77 Node cases; 562 controller cases with two live skips), and pristine protocol
compatibility passed (43 cases with 26 recovery-only skips).

Reviewed deployment merge baseline is
`86c611da94fbbb39f467eb6d47930872ec8e49fe`, retained under
`refs/andrew/deployment-bases/provider-timeout-2026-10-19`. It is a proposal merge
object, not a branch commit publishing the implementation. The candidate merges
with the last installed provider-timeout source and preserves live controls,
media and routing customizations. Owner confirmed production installation into
`/opt/andrew-letta-recovery/releases/3ed883cb4f78b353b226517bbd11d735b43f067b7b152e8cb2343e0876c7781e`.
New backup directory:
`/var/backups/andrew-local-recovery-84a7bdbf611a467c9e95e12847488d7b`.
Reconciled at backend revision 518, committed false, evidence SHA
`1476d9167c4221ba6abb55d89b9533e02a7839f9ece833b2ee2078dfde008dda`.
Enable and actual live reply verification remain pending.


Historical installation command (already completed for the backup above). Install in the current halted error state (no intervening
wake, pause, or replay):

```bash
cd /home/daniel/andrew-autonomy-media
sudo python3 scripts/install-letta-recovery.py \
  --backend-release "$PWD/.superpowers/sdd/2026-10-18-local-letta-durable-recovery/release-local-diary-recovery" \
  --repair-local-diary 5bb2f4ff78604547b595f4660f31a9c4 \
  --apply
```

The root installer independently validates the actual installed-source merge.
After success, enable using the newly printed backup directory, collect fresh
terminal/receipt evidence, then verify a fresh owner reply. P0 remains open until
that live verification; offline tests alone do not close it.
