# Local recovery acceptance — 2026-10-18

Implementation and local verification are complete for the source checkout.
Production has not been changed. The owner chose to review the final candidate
personally; that independent review is pending. Root installation must build and
test a fresh exact merge against the actual installed files.

| Check | Result |
| --- | --- |
| Node backend unit/ownership/protocol suite | 48 passed, no skips |
| Full controller suite | 533 tests, OK, 2 live-service skips |
| Full scripts suite, actual recovery release | 138 passed, no skips |
| Compatible protocols, pristine pinned SDK | 26 tests, OK, 9 recovery-only skips; 17 compatible tests ran |
| Exact merged candidate acceptance | All four stages passed; `verify_candidate(require_acceptance=True)` passed |
| Production installation/enablement | Not performed |
| Owner's independent final review | Pending |

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
| `scripts/letta-recovery-manifest.json` | `7edb83a317b7dd4c5b03cf86502887f51cb0ff4db3509c237a63f3749ac2b0e7` |

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
