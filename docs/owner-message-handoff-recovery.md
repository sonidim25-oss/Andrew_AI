# Owner-message handoff recovery

## Confirmed cause

The 2026-10-19 report identifies session
`2c9e86735bfd457180639678020ba03e`, cycle
`ea18b7577e9e4d3c82600c137c9c5636`, and controller admission
`21fa779c996a458bae0a803788745ac2`.

Andrew completed 26 provider requests and saved 29 tool responses before the
owner message arrived at 2026-10-18 23:59:52 UTC. The backend then held at
`owner_blocked` / `tool_prepared`, revision 289. The next `gmail.read` was prepared
but never admitted or executed. The controller treated owner preemption as
`local_recovery_interrupted`, then refused the owner message because that error
was set. Reconciliation subsequently labelled this `provider_uncertain` because
ten unrelated legacy reservations blocked resume despite confirmed provider
evidence for the new binding.

One earlier `python.run` has an uncertain legacy cycle-tool record. That record
is preserved: a saved invocation response does not independently establish its
external effects. The fifteen older held messages and ten older reservations
remain retained.

## Repair

A binding-specific owner-yield receipt fences further dispatch before the
backend records a handoff. At a safe boundary, prepared tools are cancelled with
`executed: false`. Controller authorization that has not reached dispatch can
also be cancelled, but only under the database lock with proof of prepared state
and no reservation. The immutable receipt binds these cancelled admission IDs;
backend checkpoint admission alone never proves that execution began. An effect
actually admitted by the controller cannot use this path. The backend
records `owner_input_pending` as the interrupted terminal reason, projects
truthful cancellation results, and requires fenced acknowledgement and
settlement before the next logical input. Confirmed results are reused.

The old model receipt remains accepted, its cycle becomes stopped, and the
queued owner message is handled as a distinct input. No model completion is
fabricated. A genuine final model response can instead complete normally, with
the owner served next. Pause, sleep, owner revision, restore, and unrelated
unknown-effect guards continue to apply to further execution.

The same path repairs an already-held `owner_blocked` checkpoint. Disabled
recovery startup preserves that journaled session without running it. Backup
restoration retains immutable owner-yield receipts. The legacy-cost guard now
accepts confirmed provider evidence as well as explicit rejection evidence,
checking newer unknown admissions first. `/status` uses one consistent snapshot;
error state takes precedence over stale reply/progress labels. Local commands
and wake-time prompt replies use routing consistent with the inbox guard and
therefore cannot trigger transient model-input handoffs. Media model commands
remain model input.

## Deployment for this exact halted incident

Keep the current owner state unchanged. The latest snapshot is already paused
at revision `32`, with no active session or cycle, while the held binding has
revision `31`. Do not send `/wake` or `/replay` before installation and cleanup.
The repair recognizes this settled pause as cancellation authority only; it never
restores the old session, cycle, or revision. The selected repair
mode verifies the original binding and current owner authority, saved waiting
message, no active
request/claims, no unsettled admitted effect, exact backend hold, and matching
result hashes. Its saved proof binds the current owner revision; changing that
revision between installation and enable makes the proof ineligible. Ordinary
installations retain their usual pause and idle requirements.

After candidate acceptance passes, run in the server terminal:

```bash
cd /home/daniel/andrew-autonomy-media
sudo python3 scripts/install-letta-recovery.py \
  --backend-release "$PWD/.superpowers/sdd/2026-10-18-local-letta-durable-recovery/release-owner-handoff-final" \
  --repair-owner-handoff 21fa779c996a458bae0a803788745ac2 \
  --apply
```

The installer validates the actual deployed controller, repeats acceptance,
stops both services, saves a paired snapshot, and rechecks the handoff before
installing with automatic recovery disabled. It preserves existing command
normalization, disabled-voice enforcement, and other deployed customizations.
After `installed: true`, use its exact printed evidence directory:

```bash
sudo python3 scripts/install-letta-recovery.py --enable /var/backups/andrew-local-recovery-REPLACE_WITH_PRINTED_ID
```

Enable revalidates the same halted checkpoint and owner state. The runtime then
performs cancellation cleanup. In the currently paused mode, Andrew remains
paused and makes no model/tool requests. After cleanup is verified, an explicit
`/wake` permits processing the already queued message; do not resend it. In the
unchanged unpaused mode, handoff processes that input automatically. Changed or active state causes a
refusal. Interrupted installation leaves evidence and stopped services for
inspection; do not bypass guards or clear receipts to force a retry.

Read-only verification:

```bash
sudo python3 scripts/diagnose-session.py --current --brief --letta-recovery-metadata
```

Verify the old scope is yielded, its old cycle stopped with the accepted receipt
retained, and the queued owner input processed with a saved reply. Verify the
prepared Gmail read was not executed. Service health or a running status alone
does not prove reply delivery or completion of an autonomous turn.

## Verification

Local real-backend tests cover owner arrival before tool execution, arrival with
a genuine final response, and the already-held incident with an uncertain legacy
Python attempt preserved. Eight process-loss scenarios cover before/after yield
receipt and controller terminal-record commits, with and without backend restart.
Prepared-tool execution stays zero; saved results are retained. Focused tests
also cover scope fencing, changed authority, unknown admitted effects, receipt
restore conflicts, and strict installer/enable preflight. No real publication,
mail access, paid model request, or production repair is performed by these tests.

Independent review found and reproduced the authorization-before-dispatch race
and the local-control/prompt race; both now have regression tests. Review of
merged candidate `ae295e76834f4d59908bf8bcad79e4a3685edda82002fe4b96a657104c8580cc`
confirmed preserved deployment customizations and found no remaining important
issues. Full acceptance passed: 62 Node tests; 556 controller tests with two
expected skips; 186 installer/recovery tests; and 38 pristine compatibility cases
with 21 recovery-only cases skipped. Evidence is in
`.superpowers/sdd/2026-10-18-local-letta-durable-recovery/candidate-owner-handoff-accepted/acceptance.json`.
The backend manifest SHA is
`7ad5d3883344a4cc8c6b24647f035dc8670990e7afedd384821066045158dac4`.
The subsequent settled-pause snapshot requires a new candidate,
`candidate-owner-handoff-paused`, SHA
`971982cff5a54d35b1791f586b7e8d69beab293ed371207dfdb66cb7fde18c69`.
Its full acceptance passed: 62 Node tests, 557 controller tests (2 skipped),
188 installer/recovery tests, and 39 pristine compatibility cases (22
recovery-only cases skipped). Its focused 32 tests and real-backend
paused-cleanup/wake/reply case passed; review found no important issues. Production installation, enable, and a successful live owner
reply remain pending.
