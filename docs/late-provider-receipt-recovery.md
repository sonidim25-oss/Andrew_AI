# Late provider receipt recovery

## Confirmed incident

On 2026-10-19, session `7cd5cca0a9fe40baaa566dd6860312e1`, cycle
`1de5a60a0b804e7fa47ee58ef3ebde5f`, scope
`9a5cbcc3a36744b2be0403fb9fc9147f` stopped at backend revision 491.
Its last step is compaction admission
`4252d848e7437886499b0dd39e9c37bfe60f6f6d1198e18bed9e1efdc9edd4db`.

The request began at 01:21:17 UTC. The backend SDK request uses a 120-second
AbortSignal and held the turn at 01:23:17. The controller continued waiting for
the upstream provider and durably saved its result at 01:28:05. The complete
22,787-byte result has one stop choice, settled usage, and matching canonical
hashes in Python and Node. The controller marks it confirmed while the backend
still says admitted/held. No current unsettled provider reservation remains.

This is a reply-transport timeout followed by a missing recovery transition;
it is not proof the provider request failed or should be submitted again.
The same turn saved 13 regular provider responses and 14 tool responses before
the stop. Three Python invocations returned execution_failed and their legacy
cycle records remain uncertain about possible external effects. Those outcomes
are independent of compaction receipt recovery and cannot be relabelled success.

## Local repair

A new capability `late_provider_receipt: 1` and private command
`recovery_reconcile_provider` permit importing only current controller evidence.
The command binds the existing input, expected checkpoint revision, provider
admission and a newly acquired connection-owned fence. It validates the exact
last provider/compaction step, request digest, result hash and settled usage.
Explicit late HTTP 429 evidence can restore backoff without issuing a retry.

Only a provider_uncertain hold from provider_admitted may change. Missing,
malformed, mismatched, unsettled or foreign proof leaves the checkpoint intact.
Identical repeated evidence is a no-op; changing it fails. The historical import
uses evidence-only controller admission. It does not project a transcript,
execute a tool, submit inference, clear owner controls or grant continuation.
The coordinator returns after import; its next pass must satisfy ordinary
scope/owner/restore and unrelated effect guards before executing more work.

Tests reproduce late completion after a detached HTTP reply using the actual
patched backend and isolated fake provider. The original model input completes
with one provider dispatch. Focused tests cover compaction, rejection/backoff,
receipt mismatches, idempotence and paused import followed by denied dispatch.
Independent review found no important issue. The full controller suite passed
559 tests with two expected skips; exact merged candidate acceptance is pending.
The patch is local and not installed. No production holds or legacy attempts
have been cleared. Full combined candidate acceptance passed below; live installation is pending.


## Timeout budgets and transport

The owner approved the combined fix on 2026-10-19. Upstream provider requests
now default to 600 seconds (10 minutes). The local backend-to-gateway provider
request has an explicit 660,000 ms (11 minute) total deadline, allowing transport
headroom. Only `/chat/completions` uses this extended path. Recovery/control and
tool calls retain their existing 120-second deadline; billing metadata retains
its separate ten-second deadline. The Python timeout is a socket timeout, not a
guaranteed total runtime cutoff; the backend deadline still bounds its wait.

The local provider path uses Node's built-in HTTP client with no retry or redirect
following, loopback-only destination checks, bounded 16 MiB response collection,
and one AbortSignal covering request and full body. This avoids a separate fetch
header deadline defeating the extended wait. Raising these limits cannot prove
anything about an unknown request; without exact settled evidence it stays held.

## Installation for the current exact halt

Full combined candidate acceptance passed. The exact installation command is
listed below.
The selected mode is `--repair-late-provider` with controller scope
`9a5cbcc3a36744b2be0403fb9fc9147f`. It checks either unchanged halted authority or a newer settled owner pause,
halted error, no current admitted effect or linked reservation, no live claims,
one released provider-held checkpoint, all invocation receipts and the exact
late provider request/result digest and settled usage. Stale reply progress may
remain from the completed gateway thread and does not authorize another call.
The installer repeats this proof after shutdown and snapshot and before enable.
It changes neither the old result nor any uncertain Python outcome.

Candidate merging uses the reviewed preceding proposal baseline
`7c8872a2494136215b6695df9d8a3d7d16a3fe3a`, retained under
`refs/andrew/deployment-bases/owner-handoff-2026-10-19`. This is a deployment merge
object, not a branch commit containing the current runtime patch.

The deployment stages remain disabled installation, separate enable, and a fresh
read-only report. On enable, receipt import can complete without inference/tool
dispatch. Continuation remains blocked if the three actual Python failure records
are still uncertain. A successful receipt import is not a claim that P0 is closed.
Do not clear those records, restore old authority, resend the request or replay
held messages to force progress. No wake is required to import historical proof.


The candidate under acceptance is `candidate-provider-timeout-verified`, SHA
`c27d1328a1adeb4148517da52c9dd6c495d76ac0280bc66e42481cd549629a17`.
The backend release manifest is
`6cb801c067ef495b965693941ad537b4e311df2f15c7f1f73a7597cfe17fe6cd`.
The preceding candidate's controller suite found an existing 120-second-contract
test; it was updated for the explicitly approved 600-second default. No runtime
change was required to resolve that test expectation. Final acceptance passed: 73 Node tests, 560 controller tests (2 skips), 194
installer/recovery tests and 40 pristine protocol cases (23 recovery-only
skips). The immutable candidate verifies with acceptance required. Independent
review found no important issue. Live installation remains pending.


Run while the recorded owner state is unchanged (no additional pause/wake/replay):

```bash
cd /home/daniel/andrew-autonomy-media
sudo python3 scripts/install-letta-recovery.py \
  --backend-release "$PWD/.superpowers/sdd/2026-10-18-local-letta-durable-recovery/release-provider-timeout-recovery" \
  --repair-late-provider 9a5cbcc3a36744b2be0403fb9fc9147f \
  --apply
```

The root installer repeats acceptance against actual deployed files before any
service shutdown. After `installed: true`, separately enable using its exact
printed `/var/backups/andrew-local-recovery-...` directory. Do not reuse the
previous handoff deployment directory. Then collect a fresh read-only diagnostic
to verify provider evidence import and the remaining uncertainty guard. This
installation does not promise that autonomous work will resume past unresolved
Python effects.


## Paused maintenance preflight — 2026-10-19

The owner has since paused Andrew at revision 34; the historical scope remains
bound to revision 33. The read-only report now finds this settled pause eligible
for historical receipt maintenance. The installer records the current pause and
revision in its exact proof and repeats the proof after shutdown and at enable.
It neither restores revision 33 nor authorizes dispatch. A real-backend test
confirms receipt import followed by denied continuation, with pause and current
revision preserved and no additional provider request. Independent review found
no important issue; 29 focused inspector/installer tests passed.

The three saved Python failures are generated-program mistakes: missing
`tempfile` import before `tempfile.mktemp`, a missing dictionary entry accessed
through `J[k]`, and `.items()` called on the list from `re.findall`. Their
possible earlier effects remain uncertain; these programs have not been replayed
or relabelled successful.

The paused candidate is `candidate-provider-timeout-paused`, SHA
`b63f1a77072a22a71902ea4404e071750cd134e9515e5c79329469841d637138`.
Only the inspector and its two test files differ from the accepted candidate.
The runtime, Node/core test inputs and backend release are identical; their
passing results are reused with exact hash comparison and provenance. Full
installer/recovery validation passed all 196 cases in 366.530 seconds. Pristine
protocol validation passed 41 cases (24 expected skips). Acceptance-required
candidate verification succeeded. Installation remains pending.

Validation note: the paused candidate's first full scripts run reported one
failure in `test_real_harness_completes_diary_and_sleep_callbacks_before_returning`
at its post-restart scheduled-wake tick (195 of 196 cases passed). That test
passed immediately in isolation without source changes. The failed log is
retained as `scripts-first-attempt-failed.txt`; a full rerun plus pristine
compatibility check is required before release. The intermittent failure's
underlying cause is not yet established. The complete rerun passed all 196 cases
without source changes; pristine compatibility passed too. Both attempts are
retained in acceptance evidence.


## Production receipt verification — 2026-10-19

Installed and enabled deployment:
`/var/backups/andrew-local-recovery-aa8249194bb04f939a40c2251a2c2800`.
Fresh read-only evidence confirms the exact compaction admission and result
digests above in backend `provider_confirmed`, revision 494, with 14 provider
steps, 14 tools and no backend claim/hold. Pause and owner revision 34 remain;
no active session/cycle or start request exists. The old repair scope is revoked
after fresh evidence-scope acquisition; one pending scope remains. The
install-only preflight no longer applies to that consumed original scope.
Autonomy still reports `local_recovery_interrupted`; the three generated Python
failures remain uncertain. Historical receipt recovery is verified; autonomous
completion and P0 closure are not. No live long-provider-request test occurred.


## Live owner handoff completed

After a fresh owner message was saved while paused, the existing installed
handoff produced backend `terminal_committed` at revision 498 with
`stop_reason: owner_input_pending`, acknowledged and settled. No controller
recovery scopes remain pending. Three uncertain Python attempts and fifteen
held messages remain preserved; owner pause/revision 34 remain unchanged.
The owner may now wake into new authority and request fresh work without replay.
This confirms retirement of the interrupted input; independent autonomous
completion remains to be observed.
