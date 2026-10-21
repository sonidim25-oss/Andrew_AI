# Memory update

## P3 session failure logging release — 2026-10-21

- Claude `4d18f23` (replaces `ffdf610`) adds three fixed warnings for failed session bookkeeping without exception text or control-flow changes. Owner authorized shipping after the historical P0 repair. Claude reports the new regression plus 118 related session tests passed; no duplicate broad run performed during integration.
- Deployment manifests bind the installed P0-0b baseline at `4e91033` to this sessions.py change. Backend and runner packages remain unchanged. Administrator rollout pending; normal coordinated acceptance runs once in the deployment command before service changes.

## P0-0 historical research rejection repair — 2026-10-21

- P0-0b is now installed/enabled at release `4e91033`: expected controller digest `a1b5fb7f47f6d6756318c241bdc4a0edc93698ce57486b36d0a09974c04aff6b` matches the public adoption record and all four services are active. The owner executed the root rollout; no additional rollout is needed for the prevention fix.
- Fresh root read-only inspection confirms the original admitted research.save call: findings 2116 > 2000, no new-entry ID, request/blob/checkpoint hashes agree, checkpoint revision 652 held/tool_uncertain, owner revision 38 versus current paused revision 39. There are 2 research entries, 17 held owner messages, 10 reservations and 56 cycle tool rows. Their exact hashes are pinned by the private proof, not checked into Git.
- Added maintenance-only tools to inspect the exact incident, save its known pre-write error, and retire the historical paused attempt. No runtime package or backend release changes are needed. Writers stop behind a startup fence; controller/backend/runner snapshots are retained. Under the correct service UIDs, the tool receipt is recorded and one atomic checkpoint migration imports it into an existing owner-yield terminal. The old providers/tools are never called again.
- A temporary controller-UID gateway exposes only evidence/result/settlement routes, blocks provider and tool dispatch, and uses only the internal systemd credential. Existing backend protocol code projects the error once and acknowledges the terminal. Owner pause/error/revision, existing research, held messages, reservations, other effects/results and provider usage are verified preserved. Normal services restart only after retirement verification.
- Validation: 10 focused tests passed, including two actual pinned-backend protocol cases; the affected eight operator/unit checks passed again after the boot guard change. Focused tests cover read-only/privacy checks, refusal on drift before service changes, retained fences after failure, exact resume, transient service cleanup, actual backend receipt/checkpoint/projection interruption and duplicate finalization, and a fresh synthetic input after explicit owner wake. No broad suite rerun. See `docs/historical-research-hold-reconciliation.md` for scope and phases.
- Production historical hold has NOT been modified by this source work. Administrator execution is required because this process cannot authenticate sudo. The repair is bound to the exact inspected admission/proof and preserves the newer pause; it does not authorize /wake or /replay.

## P0-0b journaled tool rejection results — 2026-10-21

- **IMPLEMENTED locally; review/integration pending; NOT DEPLOYED.** Branch `fix/journaled-tool-rejections` in `/home/daniel/andrew-tool-rejections-20261021`, based on merged P2-11 `239f451`. Claude reproduction `404f7ed` preserved as test-only `f02b97e`.
- Added ValueError-compatible `ToolRejected`; `ResearchValidationError` derives from it and retains its existing result format. Only audited pre-action checks raise it. RecoveryDispatch confirms the exact error result through the normal receipt path; duplicate dispatch returns that receipt. Unknown exceptions, authority validation failures and potentially post-action errors stay uncertain. Rejected sleep/wake gets no terminal-stop marker. The controller settles its cycle tool ledger for explicit rejections, preventing a second uncertainty hold.
- Audited every plain raise in the reported scope: AST finds 95 on bffca82 (controller tool body 21, memory 30, media 34, Gmail 4, skills 6), versus the approximate reported 96. The per-site rationale and separate GmailError subclass audit are in `docs/journaled-tool-rejection-audit.md`. Shared media job/artifact validators classify only explicit preflight callers; worker/post-submit callers retain uncertainty. Gmail post-send response, claim, prior-uncertainty, token and transport failures remain held.
- Moved memory provenance validation before insertion and stale research-delete validation before child updates. Diary pre-insert checks are typed; storage exceptions after execution starts remain distinct. Owner pause/sleep/restore/claim/fence/revision controls and P2-11 exact authority are preserved. Two existing fixtures were adjusted for the preflight keyword and confirmed refusal results; they still verify cleanup protection and blocked mutations.
- Before implementation: new 18-case rejection matrix errored on every rejection, while authority and post-commit controls passed. Final focused run: 208 tests pass; additional recovery/action/session/backup run: 59 unaffected tests pass plus all 9 authority regressions pass after adapting the refusal assertions. Installer tests: 73 pass. Actual pinned 0.34.13 backend: 12 protocol tests pass, including invalid sleep -> model receives rejection -> corrected sleep -> terminal completion; duplicate resume sends no additional provider call. Unknown failures after research/memory commit, media queue insertion and Gmail POST remain held and do not repeat dispatch. Receipt-write failure remains held as well.
- All three installation manifests include final source hashes and the new module. A candidate built from synthetic bffca82 deployed sources passed source/hash verification, matched all 8 changed modules byte-for-byte, and imported the controller. No production directories, services, owner controls, provider accounts, reservations or historical held effects were changed. Historical research.save reconciliation remains separate. No full core-suite rerun and no independent review claimed.
- Local validation logs: `/tmp/andrew-tool-rejections-focused-final.txt`, `/tmp/andrew-tool-rejections-authority.txt` (initial old assertion failure), `/tmp/andrew-tool-rejections-authority-final.txt` (9 corrected authority checks), `/tmp/andrew-tool-rejections-installer-final.txt`, `/tmp/andrew-tool-rejections-protocol-final.txt`.

## Interrupted journaled local-tool guards — 2026-10-21

- **DONE (implementation and review; NOT DEPLOYED):** Claude reproduction `7d56919` confirmed that an authorized continuation with `autonomy_error=local_recovery_interrupted` admitted `research.save`, then held it without a result because the Store rejected every autonomy error. This is a different trigger from the historical oversized research request, although both left an uncertain tool admission.
- Recovery dispatch now supplies temporary context for the exact authorized local callback. Research save/focus/delete and sleep/wake_at/continue may accept only this interruption error, only with matching store and operation, and only after revalidating the original scope, claim, fence, owner revision, lifecycle and restore guards. Validation acquires the SQLite writer before reading authority. Context is reset even after callback failure; the recovery error is never cleared by this exception. A redundant broad continue check in controller.py now defers to the guarded Store check.
- Nine focused authority regressions pass; 84 focused research/recovery/lifecycle/controller tests pass in total. Six actual isolated pinned-runtime tests pass, including resumed save, continue and sleep terminal confirmation plus pause and other-error controls. The original resumed-save case failed before the fix with one paid fake-provider step, no research entry and one admitted tool. Independent review approved. No full-suite rerun.
- Existing ambiguous effects, including the production historical research.save hold, remain held. No deployment, replay or owner-state changes are part of this implementation. Release pins target the integrated `bffca82` controller baseline; its frozen rollout checkout remains unchanged.

## Proven-unsent retry exhaustion integration — 2026-10-21

- **DONE (code and review; NOT DEPLOYED):** Claude `68243e2` was rebased onto worker fix `5ce3267` as `f4e0ff1`. The isolated pinned runtime established 16 failed provider dispatches over about 36 seconds before `llm_api_error`; safe legacy-mode exhaustion now schedules durable bounded retries instead of setting autonomy_error.
- The bridge's `provider_not_reached` classification is only a hint. Scheduling requires every recorded attempt for the cycle to be proven unsent, no held reservation for those attempts, no executed tool/completed turn, no journaled attempt/held owner message, and unchanged owner controls and restore guards. Existing unknown outcomes are retained.
- Review reproduced a cross-connection lifecycle race in the original patch. Both retry scheduling and admission now acquire `BEGIN IMMEDIATE` before reading guards. Two real-connection regressions failed before and passed after this correction; independent review approved it. Twenty-two focused integration tests passed. Claude's reported related/protocol results are retained without repeating the full suite.
- **OPEN in production journaled mode:** this legacy-only path does not resolve journaled provider holds or the existing research.save hold. No deployment, wake, replay or hold clearing is part of this integration. The frozen worker rollout source remains unchanged.

## Worker stop-check recovery — 2026-10-21

- **DONE (implementation and review; not deployed):** exceptions in a worker's stop predicate no longer kill its thread or consume a pending wake. Admission stays blocked until lifecycle checks succeed, with an interruptible one-second retry delay and sanitized logging. Worker status reports blocked or stopped instead of a misleading idle state.
- Seven new regression tests and 21 existing worker/unlimited-work tests passed (28 total). Independent review approved. No action is automatically replayed, no work deadline was introduced, and Claude-owned gateway.py, sessions.py and store.py were not edited.
- **DONE (deployment verified):** the reviewed provider transport candidate was installed and enabled. Public adoption is enabled and its controller digest matches the frozen `53e01cd` source. Acceptance included reused source-bound core evidence. Existing research.save hold and live-cycle verification remain open.
- The broader action-hang/watchdog candidate is still unverified and is not marked complete by this worker fix.

## Current checkpoint — 2026-10-21

- GitHub source synchronization completed at `bf9749d`; the 2026-10-20 deployment snapshot remains historical installed-source evidence.
- Claude's `e75d45c` gateway memory-context fallback is included on `fix/research-validation-result` in `/home/daniel/andrew-research-validation-20261021`. It preserves owner credit guidance and blocks when that guidance cannot be read. Its saved-result regression now exercises actual journaled receipt reuse with one provider dispatch.
- The current incident is identified: cycle `68207635b5814cd8b82b2384062c3fb4` held a `research.save` at 2026-10-20 01:45:55. Its findings had 2,116 characters against the 2,000-character limit. Exact binding/request metadata matched; no result receipt or runner mapping exists for that effect.
- The prevention fix separates pure `research.save` argument validation from storage execution. Known validation failures become sanitized, saved tool error results, allowing correction in the same cycle. Storage/callback failures remain uncertain and are not replayed.
- Validation: 698 controller tests passed (2 skipped), 49 focused research/gateway tests passed independently, and code review approved. One actual isolated pinned-backend test verified rejection, corrected save, terminal completion and duplicate-resume protection without real provider calls.
- Two-fix deployment at `14f5275` completed exact-host acceptance and administrator rollout. Public adoption is enabled, controller code digest matches the reviewed two-fix source, and controller/backend/runner services are active. Backend and runner package remained unchanged.
- The deployment preserves owner controls and does not request a wake. The existing held research.save attempt still needs a separately guarded reconciliation; no hold clearing or replay was performed. P0 remains open.

## Provider transport rollout preparation — 2026-10-21

The owner authorized deploying the reviewed transport fix and requested no duplicate full controller run. The coordinated installer now accepts explicitly selected, source-identical core acceptance evidence, with controller/tests/skills/backend/worker input hashes, interpreter version/binary hash, command and passing-log hash checked before reuse. Other acceptance and privileged UID checks remain required. Four targeted reuse tests and eighteen existing installer tests passed; the previously saved 709-test result is reused rather than rerun. Rollout completed: adoption is enabled and the installed controller digest matches the reviewed provider transport source at `53e01cd`. The old research.save hold is retained; no wake/replay is part of this rollout.

## Revised provider transport classification — 2026-10-21

- Claude commit `43d02c0` was rejected and remains unmerged: a local fault test proved that treating every URLError as unsent could release a reservation after the full POST was received and allow duplicate dispatch.
- Revised Claude commit `534d6f4`, based on `8a783ff`, uses per-call connection evidence. Only a connect failure before any application send on every connection can establish an unsent request. Transmission failures, prior redirect hops, proxy CONNECT attempts and unknown transport errors retain uncertainty. Journaled recovery remains conservative.
- Codex independently reran the original reproduction: two 422 responses, one retained reservation and exactly one provider POST. An independent reviewer approved the revised code and passed 49 focused transport/gateway/model tests.
- Full controller validation passed: 709 tests, 2 skipped. All 18 installer tests passed. Future packaging changes only gateway.py and model.py in the controller; runner/backend are unchanged. The manifest targets the installed two-fix baseline.
- These transport changes are installed and enabled; adoption and the installed controller digest were verified against `53e01cd`. They do not reconcile the existing research.save hold. Next production work remains guarded reconciliation, then explicit live owner reply and autonomous-cycle verification.

## Current checkpoint — 2026-10-20

| Item | Verified status / next action |
| --- | --- |
| Installed recovery | Coordinated runner recovery is installed and enabled. Backend release manifest: `94ccc2cc2b2d3609e52674af1676d0861a0eeba171e4bc40f70204a318b62a9d`. |
| Deployment verification | Exact host acceptance passed, including distinct service-user credentials. The interrupted schema migration was repaired; owner controls were preserved. Controller, backend, receiver and socket are active, with adoption enabled and no installation fence. |
| Live behavior | Pause was preserved. A fresh delivered owner reply and completed autonomous cycle on this release remain unverified. Earlier delivered replies are historical evidence. |
| Source synchronization | This checkpoint includes all 119 captured deployed source files, exact controller/service hashes, backend recovery extensions, installation repairs and regression tests. See `deployment/2026-10-20/`. Runtime configuration and databases stay private. |
| Next action | Complete GitHub publication, then explicitly wake Andrew and verify a normal reply and autonomous completion. |
| Existing uncertainty | Legacy provider reservations and held tool/message records remain retained; nothing was replayed. |
| Later work | Browser/destination evidence remains P0; memory consolidation/procedural memory remain P2. P0 is still open. |

The dated entries below are historical evidence. This checkpoint supersedes their older deployment instructions.

## Restore and coordinated packaging implementation — 2026-10-20

Tasks 7–8 have implementation in `/home/daniel/andrew-runner-receipts`.
Three-store restore preserves current verified execution histories and newer
controller receipt/attempt/research/message evidence. Durable external, backend
and receiver fences survive interruptions and are not cleared automatically.
Eleven new restore cases and fourteen existing backup cases passed.

The coordinated installer stages both Python packages with the pinned backend,
provisions the encrypted credential without rotation, binds administrator
adoption and service preflight records, installs with execution disabled, and
requires matching live capabilities before enablement. Owner pause/revision is
preserved; no wake is requested. Failed stop/start/install phases retain a fence
and attempt to stop all affected services. The final review resolved four
Important issues, each with a failing then passing regression; the independent
reviewer then passed 78 focused tests and approved the fix delta.

Full acceptance of `candidate-task7-8-reviewed` is running. The earlier
`candidate-task7-8-offline` snapshot is superseded. Exact-host preparation and
privileged UID acceptance remain required; sudo currently needs the owner's
password and installed controller sources are private. Production is unchanged.
No deployment, enable, wake or replay occurred. P0 remains open.

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

## Controller runner admission and atomic import implemented offline — 2026-10-19

Owner directed continuation. Tasks 4–5 now have implementation in
`/home/daniel/andrew-runner-receipts`; the production/deployment runtime files are
unchanged. The new `runner_recovery.py` owns immutable operation mappings,
pre-grant cancellation and exact result import. New execution requires explicit
store pins, an enabled adapter and the backend receipt capability marker; live
environment/adoption wiring remains part of the coordinated rollout.

Mapping plus one legacy tool attempt commit while the recovery effect remains
prepared, before any runner RPC. A ready grant rechecks current owner, scope,
lease, store identity and existing outcome/budget guards. The normal tool gate
and mapping admission share one outer transaction; no RPC holds that transaction.
The new path avoids recursive controller callbacks. A pre-grant owner stop marks
only the exact mapping/legacy attempt not_executed and fences its effect.
Admitted requests cannot use this cancellation path or be executed again.

The legacy outcome migration preserves existing tool attempts and external
receipt foreign keys; failure rolls back the table replacement. Unknown-outcome
queries explicitly exclude not_executed. Owner yield understands only proven
runner pre-grant cancellation, and an unrelated admitted tool prevents the
entire cancellation transaction from committing.

Successful result import atomically saves immutable evidence, the normal tool
result, its exact legacy settlement and one research observation using saved
session/cycle/topic/revision. Both normal replies and lost-reply recovery use the
same importer. Duplicates verify all existing records rather than replaying any
callback. Invalid/error/missing/prepared/uncertain evidence cannot import as
success. Proof checks include current store pins, exact identity/digests and the
original linked research event. Untrusted source state is retained.

Historical import can retain a result after owner pause, sleep, revision/focus
change or restore fence; it changes none of those controls. Read-only lookup is
bounded to one existing mapping per pass and occurs before the coordinator's
held-scope early return. Import does not call the backend/model or grant another
step. A real subprocess exit before/after import proves all-or-nothing records.
Three real-socket controller cases prove one execution, lost-reply lookup/import,
and zero execution if pause arrives after ready.

Focused checks passed: 23 dispatch cases, 11 import cases, 2 migration cases and
3 real-socket controller cases. Final source-pinned controller regression passed 659 tests (2 live skips),
and 19 existing real-backend protocol cases passed with no skips. Tested runtime
and test-file hashes remained unchanged. Combined 24-file review artifact:
`.superpowers/sdd/2026-10-19-runner-result-recovery/task1-5.patch`, SHA
`8c1248ddcd30c585db8fdd216056d8859796e73e5e85e35d9c9b7ffa30e15b43`.
`git apply --check` passed against the captured deployment source baseline.
Exact hashes/logs: `task4-5-implementation-evidence.json` alongside the patch. No new
production installation, owner wake, replay or task commit occurred.

Remaining: Task 6 exact held-backend tool confirmation, Task 7 three-store restore
preservation and Task 8 coordinated capability/credential/readiness adoption and
deployment acceptance. The legacy Python uncertainty and held messages remain
outside this contract. This partial isolated tree is not deployment-ready.

## Runner receipt socket handshake implemented offline — 2026-10-19

Owner directed continued implementation. Task 3 is implemented in the isolated
checkout `/home/daniel/andrew-runner-receipts`. New focused helper modules are
`runner_socket.py` and `runner_receipt_server.py`; existing client/server APIs
retain their legacy path and the receipt feature defaults disabled. Deployment
checkout runtime files and production remain unchanged.

The client checks actual SCM_CREDENTIALS on every response chunk, closes and
rejects passed file descriptors, rejects missing/truncated credentials and extra
response frames, and uses a total bounded lookup deadline. A real subprocess
inheriting its parent's listener proves the actual child sender is checked.
Distinct listener/service UIDs still require the later privileged Task 8 check.

New operations prepare before ready; only a matching fresh challenge/operation
identity and live same-boot lease can admit execution on that connection. The
lease is rechecked after admission commits. Duplicate prepared/admitted/result
operations return evidence without another grant or worker. Lookup/capabilities
remain available while a result save is blocked; execution ownership remains held
through cleanup and durable save, including handler cancellation and disconnect.
Saved success after disconnect is recoverable; failed cleanup is uncertain.
Legacy Python/browser frames retain the original serialized 64 KiB limit. The
new unlimited worker wait does not inherit the five-second connection timeout.

Twenty-seven socket/credential cases and the complete 74-test runner family
passed. The final full controller regression passed 620 tests (2 live integration
skips); source hashes remained unchanged. The combined 14-file review patch is
`.superpowers/sdd/2026-10-19-runner-result-recovery/task1-3.patch`, SHA
`bc6cf29540eee6630e16ec2b3f6a30d2454459c94c2a482141e0feeca5cc99a0`.
It passes `git apply --check` against the captured deployment source baseline.
Exact logs and hashes are in `task3-implementation-evidence.json` alongside it. No receipt credentials were provisioned outside isolated fixtures and no
production installation, owner wake or replay occurred.

Next is Task 4 controller preparation/admission plus Task 5 atomic import. Before
coding those integrations, preserve transaction boundaries: the existing
`begin_tool`/`begin_cycle_tool` helpers commit their own connection contexts and
must not split the new outer mapping/admission transaction. Extending legacy
outcomes to `not_executed` requires an FK-preserving migration and explicit
started/uncertain predicates. Exact prepared cancellation also needs a supported
owner-yield proof; merely changing an effect to cancelled would currently be
rejected by `prepare_owner_yield` as unproven. No broad hold clearing is allowed.

## Runner receipts resumed offline — 2026-10-19

Owner explicitly requested continuation after diary installation. Native sequential
implementation resumed in isolated checkout `/home/daniel/andrew-runner-receipts`,
based on a copy of the current controller/backend sources. The deployment checkout
and installed services are unchanged by this implementation. No runner receipt
feature is enabled, and legacy uncertain operations cannot use new receipts.

Task 1 shared foundation implemented: existing canonical identity functions
extracted unchanged and re-exported, immutable operation/grant/evidence types,
bounded synchronous/async frames, strict metadata and pure worker request/result
formatting. Existing output formatting remains byte-for-byte identical, including
its 15,000-character limit and untrusted-output suffix. New protocol/format tests
passed; full controller suite passed 576 tests (2 live integration skips),
runner/client/runtime/dispatch regression suites passed, and all 77 backend tests
passed. Local evidence: isolated `.runner-implementation/task1-core-tests.txt`.

Task 2 encrypted receiver store implemented in `runner_receipts.py`: explicit
store provisioning, private paths, exclusive process-owner lock, SQLite FULL
commits, AES-GCM results bound to identity/cleanup/digest, immutable identities,
64 KiB admission reservations, bounded capacity and no receipt deletion. Lookup
never creates or dispatches operations; duplicate preparation returns existing
evidence. Startup changes unfinished admissions to uncertain; cleanup failure or
error output cannot import as success. Seventeen focused tests passed, including
real subprocess exits before/after admission and result commits, capacity,
wrong key/UID, symlinks/FIFOs, corruption, retention, commit failure and restart.
The final exact-source controller regression passed 593 tests (2 live skips);
all 77 backend tests passed. Source hashes remained unchanged throughout.
Review patch and evidence are under
`.superpowers/sdd/2026-10-19-runner-result-recovery/`; the patch passes
`git apply --check` against the captured deployment source baseline.
See `docs/runner-result-recovery-implementation.md`.

Next implementation at that checkpoint was Task 3 (now recorded above),
then fenced controller dispatch/import, exact backend confirmation, three-store
restore and coordinated rollout. These remaining integrations are required before
runner receipts are deployment-ready. Do not deploy this isolated partial tree.
No task commits or production mutations were made; baseline source copies/hashes
are retained under isolated `.runner-implementation/baseline` and `baseline.json`.

## Local diary repair installed — 2026-10-19

Owner supplied successful installation output for
`/var/backups/andrew-local-recovery-84a7bdbf611a467c9e95e12847488d7b`.
Installed backend manifest/release
`3ed883cb4f78b353b226517bbd11d735b43f067b7b152e8cb2343e0876c7781e`.
Diary reconciliation succeeded at backend revision 518 with `committed:false`;
evidence SHA `1476d9167c4221ba6abb55d89b9533e02a7839f9ece833b2ee2078dfde008dda`.
Automatic resume remains disabled. Next: enable this exact new backup directory,
then inspect fresh state and verify an actual owner reply. P0 remains open.
Do not reuse the previous installation's enable directory or replay held messages.

## New owner-turn diary failure — 2026-10-19: repair validated (installation recorded above)

After wake, owner scope `5bb2f4ff78604547b595f4660f31a9c4`, revision 36,
received one confirmed provider result then called `save_diary_entry` without
an active autonomous session. The early diary guard raised after durable tool
admission, leaving no result and a backend tool-uncertain hold at revision 514.
The owner saw generic `turn_failed`. Exact real-backend reproduction confirmed
this failure. It is separate from the repaired compaction timeout. Current
owner messages 50/51 are held after failed reply, alongside fifteen older held
messages. Do not use `/wake` or `/replay` during repair.

Local fix returns a durable explicit precondition rejection before any diary
write. Existing held call uses a separate exact local-destination reconciliation
claim (`diary_not_committed`, not callback unexecuted): no diary for the bounded
Toronto interval of at most seven dates, main database device/inode/birth-time
witness predating admission (old ctime only when birth time is unavailable), then service shutdown, WAL
checkpoint, paired snapshot and repeated identity/destination proof. The root
read-only diary report confirms current eligibility observations but is not
itself a quiescent proof. New backend command consumes the exact controller
receipt, commits an error terminal and projects the tool error without inference
or callback dispatch. Owner error/controls, uncertain Python attempts and held
messages remain preserved. Resume of the same accepted candidate is supported
after the negative receipt commits, including precommit claim/release and lost
postcommit reply; no receipt or tool is repeated. Independent review found no
remaining material issue after the precommit resume regression was fixed.

New focused tests passed (controller full suite initially 562 with 2 skips;
4 Node cases; real-backend reconciliation/fault tests; installer tests). Fresh
merged full acceptance passed on `candidate-local-diary-dates`, SHA
`bc19fede38d051afa1e32ac4a9b13d498b9ba9eaba3ef43ebafc6b36b8a47cb6`.
The unchanged Node/core validation inputs were reused by exact hashes. Final
acceptance: 77 Node cases, 562 controller cases (2 live skips), 207 recovery
cases, and 43 pristine protocol cases (26 recovery-only skips). Acceptance-required
candidate verification succeeded. Production remains unchanged; sudo requires
the owner’s terminal password, so the prepared root command is the next step.
Backend release is `release-local-diary-recovery`, manifest
`3ed883cb4f78b353b226517bbd11d735b43f067b7b152e8cb2343e0876c7781e`.
Reviewed deployment proposal base is `86c611da94fbbb39f467eb6d47930872ec8e49fe`.
No new production installation or diary state mutation has occurred.
See `docs/local-diary-rejection-recovery.md`.

## Owner wake acknowledged — 2026-10-19, after handoff

Owner supplied the real wake acknowledgement: “I’m awake. Messages waiting: 2.
Held from interrupted attempts: 15; these are not replayed automatically.”
This confirms `/wake` ran; preceding ordinary messages had arrived while paused.
Both services remain active and no immediate new recovery/model error appeared
in the recent controller journal. Two saved messages await/undergo normal owner
processing; fifteen legacy held messages remain excluded from automatic replay.
Do not count the local wake acknowledgement as a provider response or proof of
autonomous completion. Next evidence is Andrew's actual queued-message reply;
avoid sending more messages/repeated wakes while it is pending. P0 remains open
until fresh work and remaining recovery scope acceptance are verified.

## Interrupted turn retired and settled live — 2026-10-19

Fresh privileged read-only report after the acknowledged owner message verifies:
backend `terminal_committed`, revision 498, stop reason `owner_input_pending`,
`acknowledged: true`, `settled: true`; controller pending recovery scopes zero.
Binding scopes now two revoked and one yielded; exact owner-yield receipt exists.
The recovered compaction result remains confirmed and the same 14 provider steps
and 14 tools remain recorded. All three Python attempts remain uncertain;
globally admitted recovery effects are empty. Owner remains paused at revision
34, one new message queued, fifteen legacy held messages preserved.

The handoff is verified, not merely inferred from an acknowledgement. It is now
appropriate for the owner to use `/wake` (normal owner command, not DB repair),
then explicitly request fresh work while retaining old attempts and excluding
/replay. Wake clears pause/error and establishes new authority; it does not
replay the fifteen held messages. The old queued message said stay paused, so a
new instruction after wake should clarify that fresh work is now authorized.
Need actual owner reply and fresh autonomous completion before claiming Andrew
works independently or P0 is complete. No additional runtime deployment occurred.

## Fresh owner message acknowledged — 2026-10-19, 22:38 Toronto

Owner supplied Andrew's actual acknowledgement: “I’m asleep or paused. Your
message is saved for when I wake.” This confirms a fresh message reached the
controller and was queued while paused. The previous diagnostic predates that
message; it cannot verify the handoff. Next step: rerun the same read-only helper
while paused to verify terminal owner yield and zero pending recovery scopes,
with all three uncertain attempts and owner controls preserved. The helper now
includes yield-receipt presence and safe terminal status metadata. No wake yet.

## Handoff blocker identified — 2026-10-19: no pending new owner input

Fresh expanded report confirms the actual owner-yield guard passes:
`paused_owner_yield_eligible: true`, `owner_guard: passed`; current agent,
conversation and configuration match. Model progress is absent. Globally
admitted effect counts are empty. The sole missing trigger is
`pending_owner_input: false`: no owner inbox items, zero queued=1/2 messages;
18 processed user messages (queued=0) and 15 legacy held messages (queued=3).
The 15 held messages intentionally do not count as fresh owner input and must
not be replayed to trigger handoff. No evidence of a stale progress/binding
regression. Need owner to send a fresh ordinary message to Andrew's Telegram
and report the bot's actual acknowledgement. Do not infer why input is absent
from the prior statement `done` or intermittent polling warnings. No further
root diagnostic is needed before obtaining that acknowledgement.

## Handoff did not complete — 2026-10-19: inspect live guard before repair

After the owner reported sending the requested Telegram message and rerunning
the diagnostic, the fresh 02:34:40 UTC report still shows pending scope
`80cd6216c25044c1986779edb647d922` held `tool_uncertain`, backend revision 494
`provider_confirmed`, owner paused at revision 34, three legacy Python attempts
uncertain. Do not describe this as a completed handoff or recommend wake.
The prior diagnostic omitted pending owner-input presence and the exact paused
handoff guard. Extended the read-only helper to report those, progress presence,
agent/conversation/config matches, aggregate queue/inbox counts and globally
admitted effect counts. It invokes the actual owner-yield guard over a read-only
connection; it performs no mutation or model/runner request. Need fresh output
to distinguish an unreceived owner message, stale progress, binding mismatch or
an unsettled effect. Telegram journal has intermittent fixed-category polling
RuntimeErrors; these do not establish whether this message arrived.

## Current hold confirmed — 2026-10-19: installed handoff applicable

Fresh privileged diagnostic identifies the sole current pending autonomous
scope `80cd6216c25044c1986779edb647d922`, held `tool_uncertain`, backend revision
494. Original scope remains revoked. Backend is still `provider_confirmed` with
the exact imported compaction result, no claim and no backend hold. Cycle
outcomes: seven returned Python operations, three uncertain Python operations,
two returned browser opens, one returned shell operation and one returned
recent-activity operation. Owner pause/revision 34 remain unchanged. This matches
the tested late-receipt + legacy failures + newer-pause handoff case.

Next operational step requires a fresh owner model message in Andrew's Telegram
while paused. Existing controller saves it; the recovery worker sees pending
owner input before its held/uncertain early return and yields the old input
without inference or tool execution. Keep unknown attempts held and do not use
/replay. Collect a fresh read-only report after handoff before requesting wake.
No new deployment is required for this installed path. New owner input must be
supplied by the owner, not injected into the database by the coding agent.

## Remaining hold investigation — 2026-10-19

Owner authorized continuation. Source confirms `_check_outcomes` treats a
cycle-linked external program returning `Status: error` as uncertain about
possible earlier side effects. The approved runner-receipt increment explicitly
covers only new successful journaled operations and cannot retroactively settle
these three legacy failures. Do not implement it as a purported cure for them.

Existing owner-handoff supports retiring the old input while retaining failed
attempts. Added an isolated real-backend regression
`test_owner_handoff_after_late_receipt_keeps_failed_tools_and_pause`; it passed
against the exact deployed release. Sequence: lost provider reply, exact late
result import, three uncertain Python attempts, newer pause, no continuation,
new saved owner input, then handoff. Asserted three attempts still uncertain,
pause/revision unchanged, no active session/cycle, no pending recovery scope and
only one provider request. This is test-only; no new runtime change/deployment.

Updated `/home/daniel/andrew-current-recovery-check.py` to report current pending
scope IDs/states/hold reasons and grouped cycle tool outcomes. Asked owner for a
fresh privileged read-only report; the currently readable report still predates
that addition. Do not recommend a live wake before checking the actual current
scope. If it matches the reproduced case, use installed owner-handoff rather
than clearing unknown attempts or redeploying another runtime patch.

## Late compaction recovery verified live — 2026-10-19

Fresh privileged read-only report after enable confirms backend phase
`provider_confirmed`, revision 494 (previously held/provider_uncertain at 491).
Exact compaction admission
`4252d848e7437886499b0dd39e9c37bfe60f6f6d1198e18bed9e1efdc9edd4db`
is confirmed with expected saved result hash
`53d1d594caf3bae981e3851195f3cdb16071ead967e38a841cb1573abd15a909`.
`expected_receipt_imported: true`; 14 provider/compaction steps and 14 tools
remain recorded, no backend claim, no backend hold reason. Owner revision 34
and pause remain intact; active session empty, active cycle absent, no session
start requested. This verifies historical receipt import, not a new long
provider call or autonomous completion. The original repair scope is revoked
(the import acquires a fresh evidence-only scope); one pending scope remains.
The install-only preflight now reports `Requires one exact pending handoff`
for that original revoked scope; this is not evidence that import failed.

Autonomy error remains `local_recovery_interrupted`; three Python execution
failures and uncertainty remain retained. Do not wake/replay or call P0 complete
on the basis of provider recovery alone. Further work must identify the current
pending scope/hold and resolve runner outcome semantics without repeating
unknown effects. The deployed timeout configuration is validated offline; no
live long-request timeout test was performed during paused verification.

## Combined fix enabled — 2026-10-19: live evidence pending

Owner supplied `enabled: true`, `owner_controls_preserved: true`,
`wake_requested: false` for deployment
`/var/backups/andrew-local-recovery-aa8249194bb04f939a40c2251a2c2800`.
Both controller and Letta services are active; controller startup checks passed.
Database/backend receipt verification requires a fresh privileged read-only
report. The existing report predates installation and cannot prove import.
No wake or replay has been requested. P0 remains open.

## Combined fix installed — 2026-10-19: enable pending

Owner supplied successful root installer output: `installed: true`,
`automatic_resume: false`, backend release
`6cb801c067ef495b965693941ad537b4e311df2f15c7f1f73a7597cfe17fe6cd`.
Exact deployment evidence: `/var/backups/andrew-local-recovery-aa8249194bb04f939a40c2251a2c2800`;
paired snapshot is its `paired-before` directory. Next step: enable this exact
deployment, preserving owner controls and requesting no wake, then collect fresh
read-only evidence of receipt import and remaining holds. Installation alone
does not verify recovery, autonomy or P0 completion.

## Paused receipt maintenance — 2026-10-19: validated, ready for root installation

The current owner pause is revision 34; the old provider-held scope is bound to
revision 33. Fresh read-only evidence reports the paused maintenance preflight
eligible. The installer now accepts that settled newer pause for importing the
exact historical receipt, while preserving pause/current revision and denying
continuation under the old authority. A real-backend test and independent review
confirm no additional provider request. All 29 focused inspection/installer tests
passed. Candidate `candidate-provider-timeout-paused`, SHA
`b63f1a77072a22a71902ea4404e071750cd134e9515e5c79329469841d637138`,
passed final validation: 196 installer/recovery cases and 41 pristine protocol
cases (24 expected skips). Acceptance-required candidate verification succeeded.
Node/core results are reused
only after exact hash checks prove their runtime and validation inputs unchanged.
No installation has occurred. The owner explicitly authorized deployment once
ready; root installation still requires their password-bearing terminal.

The three Python errors are now identified: missing `tempfile` import, missing
entry accessed through `J[k]`, and calling `.items()` on the list returned by
`re.findall`. These are generated-program failures, separate from transport.
Their possible effects stay uncertain; no replay or false success is authorized.
P0 remains open; runner implementation remains suspended.

## Combined provider fix authorized — 2026-10-19: acceptance passed, installation pending

The owner explicitly authorized longer provider waits, exact late-result receipt
recovery without resubmission, and keeping genuinely unknown outcomes held.
Implemented locally: upstream default 600 seconds; backend provider total deadline
660,000 ms using bounded loopback HTTP without redirects/retries; existing
control/tool deadlines unchanged. Exact evidence-only provider reconciliation is
implemented and independently reviewed. Targeted timeout tests and 28
installer/inspection tests passed. Full acceptance passed against
`candidate-provider-timeout-verified` (SHA
`c27d1328a1adeb4148517da52c9dd6c495d76ac0280bc66e42481cd549629a17`)
and `release-provider-timeout-recovery` (manifest
`6cb801c067ef495b965693941ad537b4e311df2f15c7f1f73a7597cfe17fe6cd`).
The first candidate stopped before scripts checks because an existing test pinned
the old 120-second default. That expectation was updated to the approved
600-second contract; its focused suite passed. Fresh merged candidate acceptance:
73 Node tests; 560 controller tests (2 expected skips); 194 installer/recovery
tests; 40 pristine protocol cases (23 recovery-only cases skipped).
`verify_candidate(require_acceptance=True)` succeeded. Independent review found
no important issue in the exact evidence path, transport or installer guards.
The fresh candidate merges with the previously installed paused-handoff source,
preserving customizations; reviewed proposal base object is
`7c8872a2494136215b6695df9d8a3d7d16a3fe3a`. The installer has an exact
`--repair-late-provider 9a5cbcc3a36744b2be0403fb9fc9147f` mode with repeated
halted-state/receipt proof and disabled activation. No new installation occurred.
Three cycle-linked Python execution failures remain uncertain and may block
continuation after provider evidence import. Do not silently settle them.

## New stop diagnosed — 2026-10-19: late compaction receipt

Fresh evidence identifies autonomous session `7cd5cca0a9fe40baaa566dd6860312e1`,
cycle `1de5a60a0b804e7fa47ee58ef3ebde5f`, held scope
`9a5cbcc3a36744b2be0403fb9fc9147f`, revision `33`. Backend revision `491`
is held `provider_uncertain` from `provider_admitted`. Its last step is
compaction admission `4252d848e7437886499b0dd39e9c37bfe60f6f6d1198e18bed9e1efdc9edd4db`.
It began at `01:21:17`; backend HTTP transport aborted at `01:23:17`
(exactly its configured 120 seconds), but the provider result was saved at
`01:28:05`. Controller evidence is confirmed, usage settled, 22,787 bytes,
one stop choice, and both Python and Node result hashes match. No new request
should be sent. No open current provider reservation remains; ten legacy ones
are retained. Thirteen ordinary provider results and fourteen tool results were
saved in this turn, so this is progress followed by a separate recovery gap.

Three cycle-linked Python attempts returned `Status: error`, fixed category
`execution_failed`, and remain semantically uncertain about possible effects.
Their controller result receipts are saved; this is distinct from the provider
transport failure. Do not mark them successful to enable continuation.

Offline reproduction confirms that late settled provider evidence cannot clear
a backend hold in the installed code. Local repair adds an exact, fenced,
evidence-only provider/compaction reconciliation command, validates request/result
hashes and settlement, and does not execute callbacks or inference. Controller
imports that evidence before continuation guards, leaving owner controls and
unrelated tool uncertainty intact. Six backend receipt tests (including interrupted commit), the paused evidence-only protocol test and 34
focused controller cases passed. The real-backend lost-reply scenario passed:
one original provider request, late result imported, same input completed.
Independent review found no important issue; separate reviewer tests passed.
Full controller verification passed: 559 tests, two expected skips.
See `docs/late-provider-receipt-recovery.md`. This
repair is not deployed. Runner receipt implementation remains suspended.

## New autonomous stop reported — 2026-10-19: fresh evidence required

After owner-observed reply delivery, the owner reports Andrew awake but automatic
activity stopped, session error `local_recovery_interrupted`, autonomous cycle
count `4`, queued/processing `0`, held messages `15`, and no next self-wake.
This does not identify the failing admission or effect. The previous read-only
report describes the successfully yielded old checkpoint and cannot diagnose
this new turn. Obtain fresh current recovery metadata before changing state;
do not wake/replay repeatedly or assume the prior handoff fix is the cause.
P0 is unresolved. Runner design/plan preparation pauses again; no runner feature
runtime implementation or production rollout has occurred. Its approved design
and prepared plan remain available for later review.

## Runner written design approved — 2026-10-19; plan ready for review

The owner explicitly approved the revised runner result recovery design. An
implementation plan is ready at
`docs/superpowers/plans/2026-10-19-runner-result-recovery.md`: eight sequential
units covering shared protocol/formatter, encrypted ledger, authenticated socket
handshake, controller dispatch, historical import, held backend confirmation,
three-store backup/restore and coordinated deployment. Native sequential
execution is recommended. Each unit specifies interfaces, failing tests and
acceptance evidence; plan review/execution-method confirmation precedes runtime
implementation. No runner feature code, dependency, service or production state
has changed. Owner reply delivery is verified; autonomous completion is not yet
verified, so P0 remains partial.

## Owner reply delivered — 2026-10-19; next runner increment under design review

The owner confirmed: “Yes, the reply arrived” after the wake instruction.
This verifies owner-observed reply delivery following the installed cleanup.
A completed autonomous turn is still unverified; P0 remains partial.

Work resumed on the next approved scope: durable receipts for newly journaled
Python/Node/shell runner results. Read-only review of the current runner and
controller found design boundaries requiring explicit treatment: receiver
preparation versus dispatch admission, ownership until cleanup and persistence
finish, cleanup failure despite successful-looking output, and exact linkage of
legacy attempts during evidence import. The written design was revised at
`docs/superpowers/specs/2026-10-18-runner-result-recovery-design.md`, including
fault cases and staged offline verification. It explicitly preserves the legacy
uncertain Python attempt and documents outcomes without an eligible result as
held. No runner feature code or production state has changed. Written-spec review
is the next stage; the implementation plan and runtime changes have not started.

## Live cleanup verified — 2026-10-19: explicit wake and reply verification next

Fresh read-only diagnostic confirms paused cancellation completed in production:
new scope `1383cbc04e10459c8d539345acc2eebc` is `yielded`; the prior scope is
`revoked`. Backend checkpoint revision `293` is `terminal_committed`, with no
hold reason, 26 confirmed provider steps, 29 confirmed tools, and one cancelled
`gmail.read`. The old model receipt remains `accepted` and cycle `interrupted`;
no completed model turn was fabricated. Owner pause `1` and revision `32` are
preserved, with no active session or model progress. Ten legacy reservations,
the legacy uncertain Python attempt, and saved owner message `48` (queued `1`)
remain retained. `local_recovery_interrupted` remains until explicit owner wake,
as intended for paused cleanup. Next: ask owner to send `/wake` once, then verify
the saved message is processed and its reply delivered, followed by completed
autonomous work. Do not resend or replay held messages. P0 remains open.

## Installed and enabled — 2026-10-19: live cleanup verification pending

Owner confirmed installation of backend
`7ad5d3883344a4cc8c6b24647f035dc8670990e7afedd384821066045158dac4`
with candidate/evidence directory
`/var/backups/andrew-local-recovery-3c5b065ca04c41a5a346102c8ecc48c8`
and paired snapshot `paired-before`. Enable returned `enabled: true`,
`owner_controls_preserved: true`, `wake_requested: false`. Read-only systemd
inspection shows both services active. These facts establish installation and
enable only. Andrew remains owner-paused at the last verified revision `32`.
Fresh database/backend evidence is required to confirm the held scope yielded,
accepted receipt and results retained, prepared tool cancelled, and no dispatch
while paused. Do not request wake until cleanup is verified. After explicit wake,
verify a delivered owner reply and completed autonomous work. P0 remains open.

## Reopened live incident — 2026-10-19: owner-message handoff deadlock

The owner reported that sending an ordinary message stopped the apparently
running agent. The fresh read-only report confirms session
`2c9e86735bfd457180639678020ba03e`, cycle
`ea18b7577e9e4d3c82600c137c9c5636`: 26 settled provider requests, 29 confirmed
journaled tools, and retained transcript progress. At 2026-10-18 23:59:52 UTC,
`Journaled model turn held (category=owner_blocked)` was followed immediately by
`Owner message queued while automatic model calls are stopped`. Owner message
48 is queued. No current provider reservation is open; only the ten older legacy
reservations remain. The recovery attempt was subsequently held as
`provider_uncertain`. The extended report confirms backend `owner_blocked` /
`tool_prepared`, revision 289, with the next `gmail.read` prepared and unexecuted.
One earlier legacy `python.run` attempt is uncertain and remains preserved;
this repair does not settle its external effects.

An isolated real-backend reproduction confirmed the handoff deadlock: incoming
owner input blocks the next autonomous dispatch; the autonomous path sets
`local_recovery_interrupted`; the owner path refuses the saved message; pending
recovery cannot resume while that message waits. Prior acceptance exercised
sequential autonomous/owner turns but missed this arrival during an active turn.
The status was sampled before the incoming message triggered the stop; its
running label did not establish completion. P0 remains open.

A separate guard bug is reproduced and patched locally: a binding with confirmed
provider evidence was still blocked by unrelated legacy reservations unless it
had previously recorded a 429. The guard now accepts either bound confirmed
provider evidence or a rejection receipt, while checking any newer admitted
unknown effects first. The regression proves a newer unknown request remains
held. This patch is not yet deployed and does not by itself repair the handoff.

The handoff repair is now implemented locally: a durable owner-yield receipt
fences further dispatch, cancels only proven undispatched work, retains actual results,
and stops the old cycle without fabricating completion. It then processes the
saved owner message as a distinct input. Real-backend normal/held handoff tests
and eight process-loss scenarios passed. Review reproduced two further paths:
owner arrival between authorization and dispatch, and local controls (including
time-prompt replies) being mistaken for model input. Both are patched and have
regression tests. Prepared, unreserved controller effects are cancelled atomically
with an immutable receipt; genuinely admitted effects remain held. Shared routing
excludes locally handled controls and time replies while retaining media model
commands. Real-backend provider/tool authorization-race tests passed. Review of
the merged candidate found no remaining important issues. Full acceptance passed
for candidate `ae295e76834f4d59908bf8bcad79e4a3685edda82002fe4b96a657104c8580cc`:
62 Node tests, 556 controller tests (2 skipped), 186 installer/recovery tests,
and 38 pristine compatibility cases (21 recovery-only cases skipped). Evidence
is under `.superpowers/sdd/2026-10-18-local-letta-durable-recovery/candidate-owner-handoff-accepted/`.
Backend release `release-owner-handoff-final` has manifest SHA
`7ad5d3883344a4cc8c6b24647f035dc8670990e7afedd384821066045158dac4`.
Production installation, enable, and a verified live reply remain pending. Candidate preparation preserves
deployed customizations using reviewed proposal baseline Git object
`a8c0f7a95c37531e11ca5943cc36a22bfa7da14e` under
`refs/andrew/deployment-bases/provider-backoff-2026-10-18`. This is an object/ref
for deployment merging, not a branch checkout or a commit of the current patch.
The installer has an exact halted-handoff mode that preserves owner revision;
keep the current owner state unchanged until installation and enable. See
[owner-message handoff recovery](docs/owner-message-handoff-recovery.md).


Deployment preflight subsequently refused because the owner state changed to a
settled pause: revision `32` (binding `31`), paused `1`, active session empty,
active cycle absent. The old checkpoint remains held; this is not permission to
restore revision `31`. The installer/runtime now have a cancellation-only paused
path: require newer owner authority, settled pause, unchanged runtime/config,
no active work or restore flags, exact held checkpoint and confirmed effects.
Normal dispatch guards remain unchanged. The enable proof binds revision `32`.
Disabled startup may mark the old cycle interrupted without invalidating this
proof. Cleanup preserves the pause, revision, accepted receipt, saved owner input,
legacy uncertainty and results; no model/tool dispatch occurs. An explicit wake
after installation and verified cleanup is required to process the saved message.
A real-backend paused-cleanup/wake/reply regression passed, 32 focused tests passed,
and review found no important issues. Full acceptance passed for
`candidate-owner-handoff-paused`, SHA
`971982cff5a54d35b1791f586b7e8d69beab293ed371207dfdb66cb7fde18c69`:
62 Node tests, 557 controller tests (2 skipped), 188 installer/recovery tests,
and 39 pristine compatibility cases (22 recovery-only cases skipped). It
supersedes the earlier accepted candidate and uses the same backend release
`7ad5d3883344a4cc8c6b24647f035dc8670990e7afedd384821066045158dac4`.
Installation and enable are still pending; keep the owner pause unchanged.

The runner-receipt feature is suspended while this incident is investigated.
Its scope was approved and a written design draft exists at
`docs/superpowers/specs/2026-10-18-runner-result-recovery-design.md`; the written
design is not yet approved. Do not describe this feature as implemented or use it
to justify clearing the current holds.

## Latest deployment and next task — 2026-10-18

The owner confirmed installation of the combined recovery fixes:
`installed: true`, `automatic_resume: false`, candidate/evidence directory
`/var/backups/andrew-local-recovery-b3c2db7a990b451cbcf2b46e5db8dddd`, with
paired snapshot `paired-before` and installed backend manifest
`6af14a32cbdee070f9d30cd0be299af2549c28bf216f4d0d8b54f2c391570322`.
Both services are active according to read-only systemd inspection. After the
enable/wake instructions, the owner reported: session running, three autonomous
cycles today, provider wait last changed four seconds ago, zero queued or
processing messages, and fifteen older held messages. This confirms active
execution, not yet a completed live turn or closed P0. Leave Andrew running
during read-only investigation; pause is needed before another installation.

The installed increment covers new journaled model-turn recovery, durable HTTP
429 backoff, the transcript projection repair, and transient restart ownership
conflicts. P0 as a whole remains partial: publications/browser/code effects,
unknown generation/provider outcomes, and legacy actions without receipts still
need their own evidence. Both P1 items are implemented; P2 remains later work.

Next task has started: investigate external-action recovery, beginning with the
runner's lost-result window. Source inspection found no durable receiver result
lookup: `RunnerSocketServer` sends the result directly; `RecoveryDispatch` saves
the controller receipt only after the callback returns. Proposed first increment
is durable runner receipts for newly journaled Python/Node/shell operations,
read-only result lookup, and exact receipt-based reconciliation without executing
the command again. Browser publications and provider-specific checks remain
separate follow-ups. Findings, alternatives, and the proposed scope are in
[runner result recovery investigation](docs/runner-result-recovery-investigation.md).
No implementation of this next increment has started; its cross-service design
requires review before changing the controller/runner protocol.

## Current incident — 2026-10-18: HTTP 429 after successful transcript projection

Historical diagnosis and preparation for the installation confirmed above.

Live service inspection confirmed the compacted-projection backend
`39f3a8fb62298b3b84c736c90de3edd25e05d010950159c3485699831e0e5044` is installed.
The latest read-only production report proves progress past the previous failure:
930 retained message entries, all seven active messages present, no missing active
IDs, and one projected recovery input. The turn started at 15:18:59 UTC and the
provider returned HTTP 429 at 15:19:11 UTC. The old gateway retained its reservation
as an unknown outcome; recovery held it as `provider_uncertain`, displayed as
`local_recovery_interrupted`. No successful autonomous turn is yet confirmed.

Current production state: owner pause is set, admission
`4177fdeadc1d4b47853b89440e6e1688` is held, cycle
`fcb564aba000404591b69a9e2d01dd83` is interrupted in execution, and there are 11
reservations: ten legacy reservations plus this request's $0.0138654 reservation.
The owner's read-only migration preflight returned `eligible: true` and matched
the unique HTTP 429 event, ID 338. This is eligibility evidence, not a repair.

The combined fix is prepared in the working tree. It durably records explicit
HTTP 429 rejections, releases only the rejected attempt's reservation, observes
`Retry-After` with exponential backoff, and retries the same provider step under
fresh authority. A crash after rejection recording can resume; a crash before
recording or an unknown response from a later retry remains held. Pause, owner
revision, pending owner input, lease loss, and restore fences still prevent
dispatch. Status now displays rate limiting and the retry eligibility time.
Backup restoration preserves and checks rejection receipts.

An explicit installation migration archives the selected old rejected input,
keeps its transcript and original receipts, records operator retirement evidence,
and releases only its matching reservation. It requires a paired backup and both
services stopped, one uniquely evidenced provider rejection, and no tool steps
or provider success. It preserves the ten older reservations and held messages.
It does not retrospectively mark the old turn completed. A new owner wake is
required after installation and enable. Interrupted migration requires inspection
of the paired snapshot and phase evidence; do not rerun by clearing guards.

Candidate: `.superpowers/sdd/2026-10-18-local-letta-durable-recovery/candidate-provider-backoff-verified`,
built against the owner's fresh production source export. Backend artifact:
`release-provider-backoff-verified`, manifest SHA
`6af14a32cbdee070f9d30cd0be299af2549c28bf216f4d0d8b54f2c391570322`.
Restart validation also reproduced a separate transient-lock classification bug:
a busy execution lock or stale checkpoint revision could become a permanent
`state_corrupt` hold. These conflicts now stay pending for the next worker pass;
unknown provider outcomes still remain held. The forced-lock regression passed. Full rebuilt candidate acceptance passed:
56 backend tests; 546 controller tests (2 skipped); 173 integration tests; and
33 original-runtime compatibility tests (16 skipped). Acceptance binds candidate
SHA `926110e799c320d853a130888feca3daa92714fb8cddad7bed1a28b1d6c447c0`.
No production code or state was changed by this session. The requested `cp -a ~/.codex ~/.codex-backup` completed.

Deployment and recovery details: [provider rate-limit recovery](docs/provider-rate-limit-recovery.md).

## Свежая независимая проверка перед deployment

По просьбе владельца выполнена независимая read-only проверка с чистым контекстом. Найден и исправлен реальный permission blocker: backend retirement от `andrew-letta` импортировал controller package, но `/opt/andrew/prototype` имеет mode 0750 и группу `andrew-control`, в которую `andrew-letta` не входит. Импорт перенесён в parent/controller-only функции; backend-child теперь использует только stdlib. Новая subprocess-проверка явно запрещает импорт `andrewbot` и успешно выполняет backend phase; retirement9 прошли. Backend artifact не менялся.

Других доказанных блокеров первого wake проверка не нашла. Осталось ограничение interrupted-install: если retirement прервётся после архивации или между backend/controller фазами, сервисы останутся остановленными, а повтор текущей команды откажется; потребуется ручная сверка сохранённого snapshot и phase evidence. Это ограничение восстановления прерванной установки, не разрешение автоматического replay. Успех production wake по-прежнему не подтверждён.

## Подтверждена причина projection_conflict; исправление подготовлено

Повторный структурный отчёт production: 929 message entries, 114 compaction entries, 7 active IDs, 6 найденных active messages, 1 отсутствующий ID нового ввода, recovery entries=0. Writer сравнивал весь сохранённый transcript с коротким активным контекстом. Теперь активный baseline связан с точными bytes transcript до синхронной записи; исходная история сохраняется byte-for-byte, добавляются только новые recovery-строки, изменение baseline или диска по-прежнему блокирует запись.

Новый backend `release-compacted-projection` проверен на реальном SDK с 929 entries и 114 compactions: новый ход завершён, история сохранена. Отдельный end-to-end сценарий воспроизвёл старую ошибку, сохранил paired snapshot, архивировал только не начатый input, затем выполнил новый wake без повтора старого ввода, сохранив 15 held messages и legacy cost reservation.

Проверки: Node51, core535 (2 live-пропуска), полный scripts160 без пропусков (251.111 с), pristine29 (12 recovery-only пропусков; 17 совместимых проверок выполнены), дополнительные retirement8 и installer16 прошли. Manifest SHA нового backend `39f3a8fb62298b3b84c736c90de3edd25e05d010950159c3485699831e0e5044`.

Installer поддерживает явный `--retire-unstarted-projection 1fac4b0c354f4822aad6b17185d704cd`: после остановки сервисов и парного backup он требует backend hold `projection_conflict` именно в `input_committed`, пустые provider/tool steps и controller effects, точный исходный input и единственный отсутствующий projection ID. Старый checkpoint сохраняется в `.history`, строка controller остаётся со state `retired`; удаляется только отсутствующий ID из active context. Любая неоднозначность оставляет сервисы остановленными. Операции над хранилищами выполняются от их штатных пользователей. Это явная отмена доказанно не начатого ввода, не разрешение replay или очистка неопределённых расходов. Применение исправления production и успешный live wake ещё ожидаются.

## Последний live wake после обновления: P0 открыт

Владелец подтвердил успешный resume-install кандидата `27babacc943743e3aacacf3c049930e0` с backend manifest `c7e99ddcccbf822213ebe67760ca3baa5a79f44fe352b4ec8c47d2523f814068`. Следующий wake 2026-10-18 14:47:56 UTC снова остановился. Свежий отчёт: input принят, backend revision 2; исходное событие `projection_conflict`, позднее controller hold `provider_uncertain`; recovery_effects пуст, новых settled requests нет. Старые 10 резервов и held сообщения сохранены. Сейчас owner pause=1.

Локально воспроизведён `projection_conflict` при законном transcript с несколькими сохранёнными версиями одного сообщения: writer ошибочно сопоставляет все исторические строки с актуальным набором. Это гипотеза причины конкретного live сбоя до получения структурной проверки production transcript, а не подтверждённое исправление. Добавлена read-only диагностика counts/active IDs/полей различий без текста сообщений; 4 новых и 10 прежних metadata-тестов прошли. Успешный live ход и закрытие P0 ещё ожидаются.

## Последний этап deployment: WAL, 2026-10-18

Владелец сообщил: кандидат `/var/backups/andrew-local-recovery-27babacc943743e3aacacf3c049930e0` остановился с `Controller WAL is not quiescent`. Это отказ перед парным snapshot и заменой runtime; оба сервиса оставлены остановленными. Успешное обновление и новый рабочий ход пока не подтверждены.

Исправлен installer: idle-проверка явно закрывает SQLite reader; после остановки обоих сервисов SQLite выполняет `wal_checkpoint(TRUNCATE)` от пользователя `andrew-control`, сохраняя committed записи. Sidecar-файлы вручную не удаляются. `--resume-apply` использует уже принятый неизменённый кандидат только до snapshot/activation, проверяя acceptance, исходники, установленный код и прежний runtime proof. После успешной установки нужен `--enable` с тем же новым backup-каталогом; owner pause сохраняется. Это исправление процесса установки, а не дополнительная возможность памяти. Root-применение ещё ожидается.

## Состояние на 2026-10-18 — восстановление установлено и включено

**Исправление первого рабочего хода подготовлено, ещё не установлено.** Повторная read-only диагностика подтвердила 10 старых резерваций за 16–17 октября, всего около $0.0944; из-за слишком широкой проверки они блокировали даже новый логический ввод до admission. Исправление разрешает только новый journaled input при распознанных legacy-резервациях без связи с recovery_effects: все старые суммы и duplicate guards сохраняются. Повтор/восстановление прерванного ввода по-прежнему блокируется при любых неопределённых резервациях. В backend новый input может принять спокойный разговор с сохранёнными legacy-tombstones, не объявляя старые вводы завершёнными; старые ID, очереди, reservations и активное исполнение не допускаются к повторному admission. Legacy effectful routes после adoption блокируются.

Регрессия реального SDK прошла: существующая история, 10 старых резерваций и 15 held messages; после перезапуска backend один новый ход завершён, старые журналы и резервации сохранены, ничего старого не повторено. Точный кандидат `candidate-legacy-admission`: Node49, core535 (2 live-пропуска), scripts139 (без пропусков), pristine27 (10 recovery-only пропусков), все acceptance-стадии прошли. Candidate SHA `e99905e39c35a34536e4fdef5f9410ff58eefd91e47400b493a7653bc15ff531`; новый backend `release-legacy-admission`, manifest SHA `c7e99ddcccbf822213ebe67760ca3baa5a79f44fe352b4ec8c47d2523f814068`. Владелец сообщил, что Andrew paused. Перед обновлением нужен обычный restart controller в paused-состоянии: он отмечает не начатый recovery-ввод interrupted без повтора; изолированная проверка сохранила все 10 резервов и 15 held messages, без transport calls. Затем `--apply` с новым артефактом, отдельный `--enable` по новому backup-каталогу; wake только после успешного обновления.

**Текущий инцидент после первого wake.** Владелец сообщил немедленную остановку: `local_recovery_interrupted`, 15 held messages, очередь/processing — 0. Установка и включение прошли, но успешное выполнение первого рабочего автономного хода не подтверждено. Первый read-only отчёт показал `provider_uncertain`, пустые recovery_attempts/effects и только prepared model-turn receipt: остановка до admission нового вызова. Код указывает на global budget reservation check. Старый brief ошибочно считал резервации только в окне текущей сессии; диагностика исправлена на global count с 20 metadata-записями, regression test с резервацией за предыдущий месяц прошёл. Для подтверждения конкретной старой резервации запрошен повторный read-only отчёт. Никакие резервации/holds не очищены. Не повторять wake/replay, не очищать holds/reservations и не объявлять старые действия завершёнными. Диагностика checkout дополнена фиксированной категорией `Journaled model turn held`, 10 metadata-тестов прошли; работающая служба не менялась.

**Владелец подтвердил успешные установку и включение.** `--apply` вернул `installed: true`; затем `--enable` вернул `enabled: true`, `owner_controls_preserved: true`, `wake_requested: false`. Рабочий кандидат и evidence: `/var/backups/andrew-local-recovery-85aecab1bf054ac1b7b1cb9cd60beb76`; парный снимок — `paired-before` в том же каталоге. Установленный release: `/opt/andrew-letta-recovery/releases/f2cd8066142130d3955900daa9b7ceec6daea900e7c4b5859c53e6fa789fd7e3`. Это подтверждение вывода установщика владельцем, а не отдельная проверка работающих служб в этой сессии. Пауза/сон сохранены; wake не запрашивался. Новое восстановление применимо только к новым журналируемым автономным ходам; старые неопределённые исходы не сняты.

**Последняя поправка совместимости с установленной версией.** Первая попытка `--apply` остановилась до изменения служб: неизвестный hash `controller.py`. После read-only экспорта текущих исходников проверены два отсутствовавших hash (`controller.py`, `main.py`) и добавлены точные разрешённые соответствия базе `f7418d2`. Сохранены нормализация команд владельца, запрет отключённого voice и startup-проверка skill-документов. Импорт cancellation перенесён, чтобы независимые изменения импортов сливались без конфликта. Кандидат из реального экспорта: `candidate-live-reviewed-v2`; 6 actual protocol и 7 installer тестов прошли. Полный core-прогон точного installed-source кандидата: 533 теста, OK (2 live-пропуска); 42 проверки incident/deployment также прошли. Предыдущая acceptance относится к историческому Git-baseline кандидату; Успешная установка через root-установщик впоследствии подтверждена владельцем выше.

**Реализация локального долговечного восстановления установлена и включена по подтверждению владельца.** Это отдельная подсистема продолжения новых автономных ходов после сбоя, а не восстановление старого неопределённого ввода. Задачи 4–8 утверждённого плана реализованы (коммиты `f05fe8b`, `ada26ed`): строгий протокол, архив последовательных вводов, защита legacy-путей, проекция транскрипта с fencing, подтверждения провайдера/инструментов, компактация, координатор в существующем model worker, парный backup/restore и установщик с безопасным rollback.

Координатор сохраняет исходный input ID, использует только точные подтверждённые результаты и не повторяет неизвестный запрос или инструмент. Пауза, сон, смена owner revision, новые сообщения владельца, restore и неопределённые расходы/эффекты блокируют исполнение. Обычные ответы владельцу могут журналироваться в том же разговоре; автоматическое восстановление таких ответов не включено. Старые legacy-dispositions могут блокировать принятие разговора; их нельзя удалять ради включения функции. История с неподдерживаемыми image/audio-блоками также удерживается.

Подтверждены 48 Node-тестов и полный controller-набор: 533 теста, OK, 2 пропуска live-интеграций. На реальных раздельных процессах backend/controller проверены 18 границ записи, 8 сценариев потери controller/обоих процессов и восстановление после подтверждённой компактации: 27 сценариев, без повторной оплаты подтверждённых шагов и без повтора подтверждённого инструмента. Отдельно проверен запуск исходного старого runtime на изолированной копии до новых admissions; живые новые журналы не затронуты. Финальный полный scripts-прогон: 138 тестов, OK, без пропусков. Acceptance точного объединённого кандидата также завершён: Node48, core533 (2 live-пропуска), scripts138 (без пропусков), pristine protocol26 (9 recovery-only пропусков; 17 совместимых проверок выполнены). `verify_candidate(require_acceptance=True)` подтверждён. Первые промежуточные кандидаты отклонены из-за отсутствовавших systemd-фикстур; исправленный `candidate-accepted` прошёл полный повторный прогон. [Полный отчёт](docs/local-letta-recovery-acceptance.md).

Кандидат `candidate-accepted` слит с проверенной исходной базой `f7418d2`; это **не** свежий экспорт production. Артефакты и полные журналы: `.superpowers/sdd/2026-10-18-local-letta-durable-recovery/`. Release manifest SHA-256: `f2cd8066142130d3955900daa9b7ceec6daea900e7c4b5859c53e6fa789fd7e3`. Candidate SHA-256: `8715da3a2b42162bf709efdd681f6ec1155b9b4c61fe36675e5bc7bd5d407529`.

Владелец выбрал самостоятельное итоговое ревью (второй вариант), затем выполнил установку и включение; отдельный отчёт независимого ревью не предоставлен. [Операционные команды и ограничения](docs/local-letta-recovery-operations.md) описывают preview, первоначальную установку с recovery-off, отдельное включение и rollback. Установщик повторяет слияние и acceptance по реальным установленным исходникам до изменения служб. Для production нужен серверный терминал с sudo: в этой сессии `sudo -n` требует пароль. Никакого `/wake`, `/replay`, платного запроса, реальной доставки или очистки holds/reservations не выполнялось.

Ниже сохранены исторические этапы; текущий статус этого инкремента указан выше.

**P0 выполнен частично. Оба пункта P1 реализованы. Оба пункта P2 остаются.**
Развёртывания ниже отмечены по подтверждениям владельца в чате; это не новая проверка работающего сервера.

### Что сделано

| Этап | Результат | Коммит | Развёртывание |
| --- | --- | --- | --- |
| P0: контрольные точки | Подтверждённые границы цикла, журнал попыток инструментов и безопасное продолжение после перезапуска без повтора завершённых действий | `747ec6b` | Подтверждено владельцем |
| P1: оригинальная история | Архив доступных исходных сообщений и результатов инструментов, `session_search`, `session_read`, ссылки на первичные записи | `045f7f8` | Входит в подтверждённое развёртывание последующих обновлений |
| P1: происхождение памяти | Классификация `owner` / `agent` / `untrusted` / `system`, неизменяемые ссылки на доказательства; внешние сведения не становятся автоматически доверенными инструкциями личности | `3ffd87e` | Входит в подтверждённое развёртывание последующих обновлений |
| P0: Gmail | Долговечные квитанции новых отправок, черновиков и перемещений в корзину; проверка результата без повтора операции; продолжение завершённого модельного хода после подтверждения | `0afe2f9` | Подтверждено владельцем |
| P0: завершение модельного хода | Привязанные к конкретному вводу квитанции Letta позволяют восстановиться, если модель завершилась, а контроллер упал при получении ответа или записи контрольной точки | `c68e128` | Подтверждено владельцем |
| P0: текстовые доставки Telegram | Квитанции отдельных частей ответов и дневников; восстановление после локального сбоя без повторной отправки подтверждённых частей; продолжение только заведомо неотправленного остатка | `2faba07` | Подтверждено владельцем после исправления теста `c14295c` |
| P0: безопасная проверка запуска | Проверка сохранённого разговора без `runtime_start`, восстановления approvals и запуска очереди до проверки состояния контроллера | `d02477b` | Подтверждено владельцем 2026-10-18 |
| P0: подтверждённые доставки медиа | Долговечные квитанции Telegram для изображений и голоса; восстановление подтверждённой доставки после локального сбоя без повторной генерации или отправки | Изменения поверх `c888e18` | Подтверждено владельцем 2026-10-18 |
| P0: локальное долговечное восстановление | Новые журналируемые модельные ходы, точные результаты инструментов, компактация, fencing, HTTP 429 backoff и исправления первого live wake | Backend manifest `6af14a32…` | Установка подтверждена владельцем; текущий ход ожидает provider, успешное live-завершение ещё не подтверждено |

Подробности: [восстановление сессий](docs/session-recovery.md), [архив истории](docs/session-history.md), [происхождение памяти](docs/memory-provenance.md).
Ранее удалённую историю инструментов восстановить нельзя; архив сохраняет доступную историю и новые события.

### Что осталось в P0

- **Live-проверка установленного восстановления.** Отдельный локальный backend-контракт для новых журналируемых модельных ходов теперь реализован и установлен. Текущий live-ход работает и ожидает provider; его завершение ещё не подтверждено. Старые ходы без точных квитанций и неизвестные эффекты не объявляются успешными по частичному транскрипту или статусу «idle».
- **Публикации и действия браузера/кода.** Нужны отдельные способы проверить результат на стороне назначения и, где поддерживается, идемпотентность. Завершение модельного хода само по себе не подтверждает неизвестный внешний эффект.
- **Генерация и неопределённая доставка медиа.** Развёрнутое обновление сохраняет положительные квитанции отправки изображений/голоса и восстанавливает уже подтверждённую доставку после сбоя контроллера. Неизвестные результаты генерации, расходы провайдера, изменение аватара и отправки без сохранённого подтверждения пока остаются удержанными. Обновление `2faba07` относится к текстовым ответам и дневникам; развёртывание квитанций медиа подтверждено владельцем 2026-10-18.
- **Неопределённые запросы к провайдеру и расчёты.** Проверять исходы и расходы по надёжным доказательствам; существующие резервации не снимаются автоматически текущими адаптерами восстановления.
- **Старые неопределённые действия без квитанций.** Не создавать доказательства задним числом. Где внешняя система позволяет, проверять вручную или отдельным адаптером; иначе сохранять неопределённость.
- **Тайм-аут Telegram без полученного подтверждения.** Bot API не предоставляет общего поиска по исходящей истории или ключа идемпотентности `sendMessage`. Это ограничение внешнего API: текущий адаптер оставляет такую попытку удержанной и не повторяет её автоматически. Нельзя обещать универсальное автоматическое разрешение этого случая.

Часть неопределённых исходов может оказаться принципиально непроверяемой. Для них сохранение удержанного состояния является корректным результатом, а не разрешением повторить действие.

### Что осталось в P2

1. **Автоматическая ревизия памяти:** объединение дубликатов, обработка противоречий, замена устаревших утверждений и история изменений с сохранением первичных источников.
2. **Процедурная память:** превращение удачных методов в проверяемые повторно используемые навыки. Сначала проверить существующие `skills` и их контракт, затем расширять их, не создавать параллельную систему загрузки.

### С чего продолжить

1. Проверка старта `d02477b` развёрнута: владелец подтвердил 2026-10-18. Новая диагностика журналов доступна из этого checkout; повторная установка сервиса для её запуска не нужна.
2. Для действительно незавершённых модельных запусков добавлена диагностика `--letta-recovery-metadata`: прямое чтение журналов interrupted / terminal / input disposition с привязкой к сохранённым ID контроллера, без вывода содержимого. Первый отчёт развёрнутого экземпляра относится к старому сбою без квитанций (разбор ниже). Он не даёт данных для привязанного восстановления. Правила consumer/ownership/authority revision изучены в закреплённых исходниках; новый изолированный эксперимент подтвердил отсутствие нужного локального interrupted-work checkpoint даже для привязанного принятого ввода. Подробности и граница следующей реализации: [исследование незавершённых ходов](docs/letta-unfinished-recovery-investigation.md). Не вызывать `runtime_start` или `sync` как диагностическое чтение и не считать исчезнувший запуск успешным.
3. Квитанции медиа развёрнуты: владелец подтвердил 2026-10-18. Исследование действительно незавершённых ходов завершено; направление поддержки в локальном backend одобрено, письменная спецификация одобрена; план реализации подготовлен и ожидает review. До неё такие ходы остаются удержанными. Проверка внешних действий и неизвестных результатов генерации остаётся отдельным следующим направлением. Консолидацию и процедурные навыки оставить последующими этапами.

### Продолжение 2026-10-18: направление backend-восстановления одобрено

Владелец одобрил предложенное направление долговечного восстановления в локальном Letta backend. Подготовлена [письменная спецификация](docs/superpowers/specs/2026-10-18-local-letta-durable-recovery-design.md): отдельный проверяемый артефакт поверх закреплённого SDK, канонические контрольные точки и точные результаты, локальное исключительное владение с fencing, чтение состояния отдельно от условного продолжения, привязка к контроллеру и учёту расходов, проверяемые установка/откат и восстановление копий. Спецификация включает crash-матрицу и сохранение удержаний при неизвестных исходах.

Статус: владелец одобрил письменную спецификацию 2026-10-18. Подготовлен [план реализации](docs/superpowers/plans/2026-10-18-local-letta-durable-recovery.md); он ожидает review и выбора способа выполнения. Рекомендуется последовательная реализация в текущей сессии, поскольку backend, протокол и контроллер используют общий контракт. Runtime-код и работающие службы на этом этапе не менялись. Самопроверка спецификации охватывает противоречия, границы эффекта, параметры хранения, гонки управления владельцем и совместимость; это проверка документа, не утверждение о прохождении новых runtime-тестов.

### Продолжение 2026-10-18: граница восстановления незавершённых ходов

Владелец подтвердил развёртывание квитанций медиа и разрешил следующий этап. Изучены закреплённые исходники Letta 0.34.13: запись interrupted-work и `recoverRecordedTurns` ограничены `conn-…`, тогда как локальный App Server использует `app-server-…`. Облачные recovery claims не дают локальному App Server долговечной исключительной аренды. `LocalBackend` наследует реестр запусков в памяти процесса. Дополнительно установлено: локальный `streamConversationMessages` вызывает выполнение хода; его нельзя использовать как безопасный поиск результата по `otid`. Этот вызов не выполнялся при диагностике.

Новый изолированный эксперимент использовал настоящий закреплённый backend, временные HOME/SQLite/хранилище и локальную имитацию провайдера. После принятия ввода и запроса внешнего инструмента процесс backend был убит до возврата результата. После перезапуска история с идентификатором tool call и привязанное состояние `started` сохранились, но interrupted-work journal отсутствовал до и после сбоя. Проверены один запрос к имитации провайдера, один локальный маркер эффекта и отсутствие нового модельного ввода при чтении сохранённого разговора. Платных запросов и реальных внешних действий не было. Это новая привязанная фикстура, а не повтор старой сессии владельца.

Результат: автоматическое продолжение действительно незавершённых ходов пока **не реализовано**. Подготовлен [отчёт с проверяемыми границами и предложением backend-контракта](docs/letta-unfinished-recovery-investigation.md): долговечные идентификаторы и контрольные точки, результаты инструментов, локальная исключительная аренда с fencing, отдельное чтение состояния и условное продолжение одного точного ввода/ревизии. Неизвестные расходы и эффекты, пауза/сон и восстановление из копии блокируют продолжение. Этот отчёт сохраняет результаты исследования; следующая письменная спецификация уже одобрена владельцем, но runtime-реализация ещё не начата. Независимая проверка исходников подтвердила эти ограничения; формулировка результата эксперимента уточнена до сохранения идентификатора tool call. Свежая проверка: оба протокольных сценария `test_turn_receipt_protocol.py` прошли с локальным Letta и имитацией провайдера — восстановление доказанно завершённого хода и проверка старта без присоединения к незавершённому запуску.

### Продолжение 2026-10-18: квитанции доставки медиа — развёрнуто

Продолжены уже существовавшие незакоммиченные изменения. Перед загрузкой медиа в Telegram сохраняются ID попытки и получатель; после положительного ответа сохраняется отдельная квитанция с ID задания, видом/MIME, хешами артефакта и запроса, ID сообщения и хешем файлового идентификатора Telegram. Текст запроса и исходные файловые идентификаторы в квитанцию не копируются. Ответ должен содержать положительный целочисленный ID сообщения, ожидаемый чат и подтверждение соответствующего типа медиа.

При старте квитанции сверяются до восстановления сессий. Совпавшее подтверждение закрывает локальную доставку без новой генерации, загрузки и уведомления о неопределённости. Фоновая сверка обрабатывает удержанные доставки и не завершает активную отправку. Смена попытки, получателя, артефакта, запроса или типа медиа не допускает подтверждения чужим ответом. Флаг восстановления из резервной копии блокирует сверку; пауза/сон не снимаются. Старые отправки без квитанций, тайм-аут и ошибка записи квитанции остаются неопределёнными без автоматического повтора.

Исправлена совместимость `scripts/media-live-sample.py`: ручной путь отправки теперь тоже сохраняет привязанную квитанцию перед подтверждением. Его тест использует полный ответ Telegram и проверяет, что повторный запуск не генерирует и не отправляет образцы заново. Сам скрипт с реальным провайдером не запускался.

Свежие локальные проверки: основной набор — 511 тестов, OK (2 пропуска); 10 тестов квитанций медиа прошли, включая отдельный процесс с `os._exit` после записи квитанции и до завершения доставки; тест ручных образцов прошёл. Проверены слияния с семью предыдущими релизами и семью ранее проверенными снимками развёрнутых обновлений; независимые изменения владельца сохранены. Старый исходный экспорт `/home/daniel/andrew-deployed-source` сам по себе не является актуальной установленной версией: прямое слияние с ним отклоняется из-за пересечения в `appserver.py`. Для проверки кандидата использованы сохранённые проверенные снимки обновлений, без изменения манифеста для обхода этого отказа.

Полный набор `scripts/tests` не является зелёным: в `test_owner_timeout_patch.py` подтверждены прежние 1 failure и 5 errors, воспроизводящиеся и на неизменённом `HEAD=c888e18`. Фикстура ищет старую сигнатуру `reply(..., timeout=None)`, хотя текущий метод содержит дополнительный параметр `origin`; замена тайм-аута в тесте не происходит. Это отдельный дефект старой фикстуры, не исправление рабочего тайм-аута. Тест образцов медиа, падавший при продолжении работы, исправлен и проходит. В объединённом кандидате основной набор также завершился с OK: 511 тестов, 2 пропуска. В исходниках прошли все 17 протокольных тестов с локальным Letta 0.34.13 и имитацией провайдера; 12 тестов установщика и 10 тестов диагностики тоже прошли. Отдельная локальная проверка подтвердила миграцию старой схемы медиа без создания вымышленных квитанций и вывод диагностических метаданных без содержимого и изменения SQLite. В объединённом кандидате также прошли все 17 протокольных тестов. Все эти проверки использовали временное локальное состояние и имитации внешних систем.

Независимый обзор кода не выявил критических или значимых замечаний. Неблокирующий пробел: полезен отдельный интеграционный тест порядка запуска `main.py`, где квитанция медиа снимает только препятствие доставки для подтверждённой сессии, а отсутствие квитанции и пауза/сон по-прежнему блокируют продолжение. Текущие проверки покрывают эти условия на границах сервисов, но не весь порядок запуска одним сценарием.

В ходе локальной проверки работающий контроллер, сообщения, резервации и журналы Letta не менялись; платных запросов и реальных отправок не было. После проверки владелец подтвердил развёртывание обновления 2026-10-18; это подтверждение владельца, а не новая проверка служб.

Новое обновление проверки старта: 501 основной тест (2 пропуска), 17 протокольных тестов и 12 тестов установщика прошли. Основные проверки и протокольные проверки также прошли на кандидате, объединённом с экспортированным развёрнутым кодом. Проверены шесть предыдущих этапов обновления; независимые изменения владельца сохранены. Новый протокольный тест оставляет настоящий локальный запуск Letta незавершённым, дважды проверяет старт замещающего контроллера без присоединения/нового запроса/отмены и затем разрешает завершиться только исходному запуску. Платных вызовов и реальных отправок не было.

Последняя проверенная версия `2faba07`: 499 основных тестов (2 пропуска), 16 протокольных тестов, 12 тестов установщика и 42 проверки incident-fix прошли. Основные и протокольные проверки также прошли на кандидате, объединённом с экспортированным развёрнутым кодом. Проверены пути обновления от предыдущих этапов; независимые изменения управления владельцем сохранены. Использовались локальные имитации, без платных модельных вызовов и реальных отправок сообщений.

Последняя попытка установки Telegram-обновления: `/var/backups/andrew-autonomy-media-k70bytdp`. Установщик остановился до изменения сервисов: один тест прочитал статус цикла до завершения очистки в отдельном модельном работнике. Тест исправлен: он явно задерживает очистку, проверяет независимую доставку уведомления о паузе, затем ждёт завершения именно модельного цикла и проверяет `stopped`. Проверки отмены и запрета нового модельного вызова сохранены. Исправленный тест прошёл 10 последовательных запусков; все 499 основных тестов (2 пропуска) прошли в исходниках и в объединённом кандидате; протокольная проверка отмены Letta и 12 тестов установщика также прошли. После исправления `c14295c` владелец подтвердил успешное развёртывание Telegram-обновления.

Команда развёртывания последнего обновления, если оно ещё не установлено:

```bash
sudo python3 /home/daniel/andrew-autonomy-media/scripts/install-autonomy-media.py --apply
```

Диагностика после развёртывания:

```bash
sudo python3 /home/daniel/andrew-autonomy-media/scripts/diagnose-session.py --current --brief
```

Диагностика долговечных журналов Letta (локальное чтение файлов, без подключения к SDK, модельных запросов, подтверждений или повтора действий):

```bash
sudo python3 /home/daniel/andrew-autonomy-media/scripts/diagnose-session.py --current --brief --letta-recovery-metadata
```

По умолчанию используется HOME службы Letta `/var/lib/andrew/letta`; для другого расположения есть `--letta-home`. Отчёт ограничен пятью квитанциями и 16 пространствами имён каждого вида; файлы больше 1 MiB не читаются. Выводятся только фиксированные метки, количества и совпадения идентификаторов; содержимое сообщений/инструментов и данные владения не выводятся. Пропавший файл, `replayCompleted`, `started` и даже совпавший terminal не разрешают продолжение. `authority_verified` и `automatic_resume` остаются `false`; несколько файлов не являются атомарным снимком. Проверены повреждённые записи, чужой scope/ввод, symlink/FIFO, ограничения размера и отсутствие записи в SQLite. Новая диагностика не меняет службы и не требует повторной установки. Владелец предоставил отчёт работающего сервера 2026-10-18 (разбор ниже). Проверки: 9 тестов диагностики, 2 реальных протокольных сценария с локальным провайдером, 12 тестов установщика и 42 проверки incident-fix прошли.

Отчёт владельца 2026-10-18: сохранённый сбой произошёл **2026-10-17 19:47:01 UTC**, категория `context_compaction_failed`; это не свидетельство нового сбоя после развёртывания `d02477b`. Контроллер находится в `error`, активный session ID пуст, автоматические вызовы остановлены. В этой сессии нет cycle checkpoints, tool attempts и model-turn receipts; `controller_receipt_count=0`. Поэтому `records=[]` не означает, что журналы Letta пусты: предыдущая диагностика перечислила пространства имён, но без привязанных ID не читала записи. Исправлено: теперь такой случай возвращает `no_controller_receipts` и `not_attempted`, не обращаясь к журналам. Старому сеансу нельзя задним числом выдать доказательство завершения. Сообщение №47 остаётся `uncertain`; три запроса учтены, открытых резерваций нет. Отчёт не содержит точной причины неудачной компактации и не доказывает потерю сохранённых исследований. Автоматического wake/replay/очистки не было. Исправление диагностической метки проверено 10 тестами диагностики; службы не менялись.

Не выполнять автоматически `/wake`, `/replay`, повторные отправки или очистку удержанных сообщений и резерваций. Следующая работа должна сохранять паузу/сон владельца и существующие неопределённые исходы.

## Исходный список и обоснование — историческая запись

Ниже сохранена первоначальная оценка до реализации. Формулировки «не подтвердил» и «нужен механизм» в этой части не являются текущим статусом; актуальный статус указан выше.

## Чего не хватает или что стоит улучшить

### P0 — Восстановление незавершённых действий

В `sessions.py` незавершённый цикл после аварии переводится в состояние ошибки, чтобы не повторить потенциально выполненное действие.

Это безопаснее слепого повтора, но означает, что Andrew часто останавливается и ждёт `/wake`.

Нужен механизм продолжения с последней подтверждённой контрольной точки, без повторной отправки писем, публикаций и других внешних действий.

### P1 — Поиск по оригинальной истории — идея Hermes

В Andrew уже есть FTS5 для памяти. Но я не подтвердил отдельный инструмент, аналогичный `session_search` Hermes, который позволяет найти исходные сообщения предыдущих сессий.

Это особенно полезно, когда сжатая память говорит «я уже проверил документ», но доказательства не сохранились в основной памяти.

Hermes хранит историю сессий в SQLite и позволяет искать по ней отдельно от краткой памяти.

Упомянутый источник: reddit.com.

### P1 — Происхождение и доверие к воспоминаниям — идея OpenClaw

Andrew уже хранит ссылки на события и дневники, но в изученной схеме `memory_records` нет отдельной строгой классификации происхождения вроде `owner`, `agent`, `untrusted`, `system`.

OpenClaw использует такую классификацию, чтобы сведения с сайтов и из сообщений посторонних не превращались автоматически в доверенную основную память.

Упомянутый источник: OpenClaw.

Это особенно важно теперь, когда Andrew читает Moltbook и взаимодействует с другими агентами.

### P2 — Автоматическая ревизия памяти

В OpenClaw есть отдельный процесс консолидации: отбор полезных сведений, объединение дубликатов, замена устаревших фактов и сохранение истории изменений.

В исследованных компонентах Andrew я не подтвердил аналогичный полный механизм.

Здесь полезна не просто автоматическая суммаризация, а пересмотр утверждений с сохранением первичных источников.

### P2 — Процедурная память — идея Hermes

Когда Andrew находит удачный способ работы, например сохранять PDF вместе с URL и SHA-256, он должен иметь возможность превращать его в проверяемый навык.

Такой навык можно загружать при необходимости, вместо постоянного включения инструкции в основную память.

Нужно отдельно проверить существующую реализацию `skills`, прежде чем что-либо добавлять.

## 4. Главная проблема, которую показывает код

В `main.py` основной цикл вызывает `dispatch.step()`. При этом отдельные работники обслуживают Telegram, ответы и модель, но часть операций инструментов остаётся синхронной.

В документе `docs/RELIABILITY_REVIEW_2026-10-15.md` прямо отмечено, что вызовы инструментов могут занимать до 120 секунд и иногда заканчиваются тайм-аутом до завершения очистки ресурсов.

Кроме того, при неопределённом результате выполнения Andrew намеренно не повторяет действие автоматически.

Это объясняет часть твоих наблюдений:

1. Andrew выполняет длительное действие.
2. Происходит тайм-аут или авария.
3. Система не знает, завершилось ли действие.
4. Автономная работа останавливается.

Это не обязательно дефект реализации: остановка при неопределённости защищает от повторных внешних действий. Но для долгосрочной автономности нужен способ безопасно разрешать эту неопределённость.

## 5. Что внедрять первым

Я бы предложил такой порядок:

| Приоритет | Изменение | Ожидаемая польза |
| --- | --- | --- |
| 1 | Контрольные точки и безопасное возобновление | Меньше остановок после сбоев |
| 2 | Поиск по первичным сессиям, как в Hermes | Меньше потерь доказательств |
| 3 | Классификация доверия, как в OpenClaw | Защита памяти от внешних инструкций |
| 4 | Консолидация с историей изменений | Меньше устаревших и противоречивых записей |
| 5 | Процедурные навыки | Повторное использование приобретённых методов |

Я бы не начинал с пункта 4. Автоматическая консолидация увеличивает сложность и создаёт дополнительные возможности для ошибок. Сначала нужно обеспечить сохранность исходных данных и стабильное восстановление.

Validation note: the paused candidate's first full scripts run reported one
failure in `test_real_harness_completes_diary_and_sleep_callbacks_before_returning`
at its post-restart scheduled-wake tick (195 of 196 cases passed). That test
passed immediately in isolation without source changes. The failed log is
retained as `scripts-first-attempt-failed.txt`; a full rerun plus pristine
compatibility check is required before release. The intermittent failure's
underlying cause is not yet established. The complete rerun passed all 196 cases
in 366.530 seconds without source changes; pristine compatibility also passed
(41 cases, 24 expected skips). Both attempts are retained in acceptance evidence.

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

## Privileged host preparation passed — 2026-10-20

Owner supplied successful preparation output for
`/var/backups/andrew-runner-recovery-candidate-v2`. All acceptance checks,
including distinct runtime UID credentials, reported passed. Production is
unchanged. The installer will reverify the exact private candidate before
activation. Next: owner `/pause`, coordinated initially disabled installation,
then capability-checked enablement. No wake or replay is requested. P0 remains
open until live reply and autonomous completion are verified; later P0 evidence
scopes also remain.

## Installation interrupted at controller schema migration — 2026-10-20

Actual accepted packages are installed but all four affected services remain
stopped with the durable installation fence. Owner inspection confirms pause1,
execution0, unchanged owner controls, zero receiver rows, original backups
retained, absent adoption and absent runner controller tables. The service
working directory shadowed the controller PYTHONPATH in python -c, importing
the older service Store. Explicit role working directories and schema assertions
fix future installer invocations.

A separate guarded `scripts/resume-runner-installation.py` loads the immutable
accepted installer, validates exact code/config/backups/key/pins/empty receiver,
retains current stores durably, migrates from the explicit controller package,
creates a fresh three-store snapshot and finishes DISABLED adoption. It never
starts the controller or replays work. Independent review approved the concrete
repair; its one parent-directory fsync finding was fixed RED to GREEN.
17 installer and 5 repair test methods with refusal/fault subcases passed.
Evidence: `installation-schema-repair-evidence.json`. Actual root repair remains
pending. Do not rerun --apply against this partially installed candidate.

After successful repair, enable must use the immutable accepted installer from
a controller working directory (root shell cd /opt/andrew/prototype), because
the worktree manifest now binds the future installer correction. Explicit owner
wake, reply delivery and autonomous completion remain unverified. P0 is open.

Repair preflight refused source permissions before any database changes. The
packager copy retained group-write developer modes 775/664, while the repair
requires protected root-owned source paths. Future staging now strips group/
other write bits; regression RED to GREEN and all 18 installer tests passed.
Existing private root candidate requires chmod -R go-w on its source tree only;
this preserves all accepted content hashes. Then rerun the same guarded repair.
Repair tool remains unchanged (SHA 77025e65d2b3af99bf9d20e0509310c30ffd02603bdd96c4a84ef567ab0b3c55).

## Schema repair installed disabled — 2026-10-20

Owner reports installed true, automatic_resume false and execution_enabled false.
Repair evidence: /var/backups/andrew-runner-recovery-candidate-v2/repair-fd38e9b29ebf4c81bb14382e440a6ea0.
Read-only host checks independently show backend/receiver/socket active, controller
inactive, adoption disabled and installation fence absent. Next is capability-
checked enablement through the immutable accepted installer, invoked by root
from /opt/andrew/prototype to avoid caller package shadowing. Preserve pause;
no wake or replay has been requested. Live reply/autonomous completion remain
unverified, and P0 remains open.

## Runner recovery enabled, live verification pending — 2026-10-20

Owner reports enabled true, owner_controls_preserved true and wake_requested
false from the immutable accepted installer. Independent read-only checks show
controller/backend/receiver/socket active, adoption enabled and no installation
fence. Enablement preserved pause. Next: explicit owner /wake, acknowledge wake,
then a short normal owner message; confirm a delivered reply and subsequently
one completed autonomous cycle. Service activity alone does not prove model
progress. Existing held attempts stay held; no replay occurred. P0 remains open.
