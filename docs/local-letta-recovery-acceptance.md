# Local recovery acceptance — 2026-10-18

Implementation and local verification are complete. The owner reported successful
production installation (`installed: true`) and subsequent enablement
(`enabled: true`, `owner_controls_preserved: true`, `wake_requested: false`).
This records the installer output supplied by the owner; no separate live-service
inspection was performed in this session. Evidence and paired snapshot are under
`/var/backups/andrew-local-recovery-85aecab1bf054ac1b7b1cb9cd60beb76`.
The owner chose personal review and proceeded with installation; no separate
independent review report has been recorded.

| Check | Result |
| --- | --- |
| Node backend unit/ownership/protocol suite | 48 passed, no skips |
| Full controller suite | 533 tests, OK, 2 live-service skips |
| Full scripts suite, actual recovery release | 138 passed, no skips |
| Compatible protocols, pristine pinned SDK | 26 tests, OK, 9 recovery-only skips; 17 compatible tests ran |
| Original Git-baseline candidate acceptance | All four stages passed for that historical candidate; superseded by the installed-source compatibility correction below |
| Production installation/enablement | Both succeeded, reported by owner |
| Owner's independent final review | Owner proceeded with deployment; separate report not recorded |

The 27 real process-loss cases comprise 18 before/after backend durable
boundaries, 8 controller-only/combined-loss cases, and one confirmed compaction
case. Both actual processes and their in-memory transport credentials are lost in
the combined cases. The tests prove exact invocation counts and retained input
identity; they are not simulations of a restart using only mocked state.

The rollback protocol test also starts the actual pristine older SDK using only
an isolated proven pre-admission snapshot. It verifies zero provider calls and
unchanged live new-contract journals. Unit checks prove unsafe rollback never
starts either service, unknown installed source is rejected before service
mutation, and initial activation keeps automatic recovery disabled. Root-owned
artifact installation, real systemd startup/proof renewal, and the actual live
merge still require the server-side preview/apply procedure.

The two controller skips are
`AppServerWebSocketTests.test_running_server_advertises_persistent_agent_operations`
and `LiveLettaIntegrationTests.test_live_agent_turn_and_memory_survive_client_or_service_restart`.
They require live service/provider access and were not enabled. The pristine run
skips the six `LocalRecoveryProtocolTests` and three
`LocalRecoveryCrashProtocolTests`, which require the separate recovery artifact.
All nine ran in the recovery-release scripts suite.

## Artifact identity

Private evidence directory:

```
/home/daniel/andrew-autonomy-media/.superpowers/sdd/2026-10-18-local-letta-durable-recovery
```

| Artifact | SHA-256 |
| --- | --- |
| Pristine SDK bundle 0.34.13 | `eab90b26ded2647769a00ae29ec95fea1881173da53e2d54f620fc6df085b2fe` |
| Separate patched bundle | `21662278772607943acf2d2acc365e0e25918d26d0c8342365296c50bd90a0c2` |
| `release-candidate/recovery-release.json` | `f2cd8066142130d3955900daa9b7ceec6daea900e7c4b5859c53e6fa789fd7e3` |
| `candidate-accepted/candidate.json` | `8715da3a2b42162bf709efdd681f6ec1155b9b4c61fe36675e5bc7bd5d407529` |
| Original deployment manifest (historical) | `7edb83a317b7dd4c5b03cf86502887f51cb0ff4db3509c237a63f3749ac2b0e7` |

Bundle SHA alone is insufficient: the bundle imports the separately hashed
extension. The release manifest binds the full self-contained dependency tree.
Candidate acceptance also binds controller files, test inputs and manifests.
The local merge baseline is the reviewed Git revision `f7418d2`, not a current
production export. Implementation commits are `f05fe8b` (execution/coordinator)
and `ada26ed` (packaging, snapshots and process-loss acceptance).

The first two interim candidate copies failed the existing browser installer test
because their test tree omitted the systemd fixtures. They were not accepted.
The corrected candidate includes those fixtures; the existing browser suite
passes all six tests.

Source-checkout evidence: `gap-node.txt`, `final-core.txt`, `final-scripts.txt`,
and `final-pristine-protocol.txt`. Candidate evidence is retained in
`candidate-accepted/*-acceptance.txt` and `acceptance.json`. Files and test
stores contain only disposable fixture data; paired operational snapshots are
separately private and must not be published.

## Limits preserved by design

- The old interrupted input lacks new-contract receipts and remains held.
- An unknown provider/tool outcome remains held; unknown provider spend retains
  its reservation. No global reservations or unrelated held messages are cleared.
- Legacy accepted-input records can block adopting an existing conversation.
  They are retained, including records whose transcript appears complete.
- Automatic recovery covers autonomous turns, not interrupted owner replies.
- This adapter accepts typed text/tool history. Unsupported image/audio history
  holds; it is not silently flattened or discarded.
- Storage limits fail closed. Referenced historical receipts/blobs are retained;
  this release does not automatically prune settled conversation history.
- Restore requires separate reconciliation of both stores and retains pause.
- Installation starts with recovery off; enablement does not wake Andrew.

[Operator procedure](local-letta-recovery-operations.md) contains concrete preview,
apply, enable, diagnosis and rollback commands. No paid provider request, real
external effect, wake/replay, or production service mutation was used to obtain
these results.

## Installed-source compatibility correction

The first production apply attempt stopped before service mutation because its
installed controller hash was not in the reviewed manifest. A new read-only
export at `/home/daniel/andrew-recovery-live-source` was then reviewed. Two exact
installed hashes were added, both against merge ancestor `f7418d2`:

- `controller.py`: `99443e0964000fa256d0680e3e9bf66f23127189bed3542f989f13a7d1fe18de`
- `main.py`: `397784d8c7bbc3bbd03a44ae3b861dd7c9c41c98ddb8b2766c4aebdb412218c6`

Their differences preserve owner-command normalization, the voice-disabled guard,
and trusted skill-document startup validation. The recovery cancellation import
was moved away from the adjacent menu import so Git can merge both independent
changes without a conflict. No behavior was removed. The resulting package also
preserves the exported `telegram_menu.py` and `skills.py` byte for byte.

The exact installed-source merge is retained in `candidate-live-reviewed-v2`,
candidate SHA-256 `59a90ef29139e50d6253fdcca74194873104f943fe7f36e3a6f3ca103e03ded4`.
Its six actual recovery protocol tests and seven recovery installer tests passed.
The full controller suite passed 533 tests (2 live-service skips), recorded in
the adjacent `live-core-check.txt`; 42 incident/deployment checks also passed.
The current deployment manifest SHA-256 is
`ffe5fbc7fcb2eb4a74c2673cb6c5f0a82964ae5f42966a193344c5df5bd95c98`.
The backend artifact is unchanged. Earlier candidate acceptance files remain
historical evidence and do not authorize this new manifest. The root installer
must repeat the complete acceptance run against the then-current installed merge
before it changes services; unknown additional source hashes still refuse.

## First live turn: legacy reservation admission fix

After successful installation/enablement, the first live wake was held as
`provider_uncertain` before creating any recovery scope or effect. Read-only
diagnostics confirmed ten older legacy reservations (October 16–17), total
approximately $0.0944. The prior diagnostic incorrectly reported only reservations
in the current session window; it now reports global metadata.

The correction distinguishes fresh logical input from interrupted-turn recovery.
Fresh admission can retain recognized legacy provider reservations (UUID or
provider-SHA identifiers) unlinked to new-contract effects. No cost/reservation
is cleared or settled. Unknown/new-contract reservations still block fresh input;
all outstanding reservations still block interrupted-turn resume. Adoption of a
quiescent conversation can retain older nonreplayable legacy input tombstones,
while refusing the same old input ID, queued replay payloads, durable input
reservations and active execution. Adoption fences all legacy execution routes;
retained markers never certify old completion.

The actual SDK regression first runs an old-format turn, restarts the backend,
then completes one distinct new turn with ten older reservations and fifteen
held owner messages. The old disposition bytes, reservations and held messages
remain; original history is present, and legacy runtime_start is rejected after
adoption. Existing unknown-outcome process-loss cases still pass.

Exact corrected deployment candidate: `candidate-legacy-admission`, SHA-256
`e99905e39c35a34536e4fdef5f9410ff58eefd91e47400b493a7653bc15ff531`.
Acceptance: Node49 (no skips), core535 (2 live skips), scripts139 (no skips),
pristine protocols27 (10 recovery-only skips, 17 compatible checks executed).
Release directory: `release-legacy-admission`; full release manifest SHA-256
`c7e99ddcccbf822213ebe67760ca3baa5a79f44fe352b4ec8c47d2523f814068`.
This correction is locally accepted and is not yet reported installed. The owner
reported pause. A normal paused controller restart safely retires the blocked
unadmitted cycle before installer idle checks; a separate isolated check retained
all ten reservations and fifteen held inputs without any transport call. Root
apply must use the new release, then enable using the newly printed backup path.

The owner's root apply produced candidate
`/var/backups/andrew-local-recovery-27babacc943743e3aacacf3c049930e0`
and then refused the pre-snapshot WAL check. The installer correction closes
its own idle-check reader and checkpoints SQLite after both services stop.
Focused tests exercise committed WAL preservation into the paired snapshot,
refusal with an active reader, closed idle connections, failure before any
replacement/start, and reuse of exact accepted candidates with unchanged proof.
The controller/backend runtime and release manifest remain unchanged by this
installer correction. Root retry and successful first live turn remain pending.

## Compacted transcript failure observed after successful WAL retry

The owner confirmed candidate `27babacc943743e3aacacf3c049930e0` installed with
backend manifest `c7e99ddcccbf822213ebe67760ca3baa5a79f44fe352b4ec8c47d2523f814068`.
Its next live wake accepted input, then reported `projection_conflict`. Structural
read-only evidence found 929 message entries, 114 compaction entries, six active
messages and one missing projected input ID; no recovery effect was recorded.
The previous writer incorrectly compared all transcript entries with active
context. A later reconciliation's `provider_uncertain` label did not identify
the original projection error.

The corrected writer binds active messages and exact on-disk bytes before the
synchronous SDK rewrite. It appends only new recovery entries while retaining
the original transcript bytes. Changed active messages or disk bytes still
refuse projection. The pinned SDK regression retains 929 entries including 114
compactions, completes fresh work, restarts, and reads the completed answer.

A separate actual-SDK regression injects the prior raw-prefix failure, pauses,
takes and verifies a paired snapshot, archives only the proven unstarted input,
and then completes a new wake on the corrected release. The prior checkpoint
remains inspectable as held; the original transcript, 15 held messages and legacy
cost reservation remain. No previous input is replayed. Retirement unit tests
also reject any backend step, controller effect admission, changed evidence,
already projected input, missing pause, or unsafe archive symlink. Installer
tests require the paired snapshot before retirement and leave services stopped
when retirement refuses.

Release: `release-compacted-projection`, manifest SHA-256
`39f3a8fb62298b3b84c736c90de3edd25e05d010950159c3485699831e0e5044`.
The release passes strict verification against the pinned bundle and current
extension sources. Node51 passed; core535 passed (2 live skips); pristine29
passed (12 recovery-only skips, 17 compatible checks executed). Retirement8 and
installer16 focused tests passed, including the actual-SDK repair regression.
The full scripts run is recorded separately in local evidence. This release is
not yet reported deployed; P0 remains open until a successful live turn.
