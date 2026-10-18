# Unfinished local Letta turns: recovery investigation

Investigation date: 2026-10-18. This records a feasibility investigation, not an
implemented resume adapter. The media delivery update was confirmed deployed by
the owner on the same date. No production service, held input, provider
reservation or journal was changed during this investigation.

## Result

The pinned local App Server does not provide the durable interrupted-work and
ownership contract required for safe automatic continuation after backend loss.
Matching accepted-input metadata alone is insufficient. Andrew must continue to
hold genuinely unfinished turns; successful media delivery recovery does not
change this conclusion.

This conclusion is limited to the examined `@letta-ai/letta-code` **0.34.13**
bundle, SHA-256
`eab90b26ded2647769a00ae29ec95fea1881173da53e2d54f620fc6df085b2fe`.
It makes no claim about newer releases or a cloud backend. Source module names
below refer to the source sections embedded in that exact bundle.

## What the pinned source establishes

| Boundary | Evidence | Consequence |
| --- | --- | --- |
| Local App Server identity | `src/websocket/app-server.ts`, `startAppServer`: the owned listener uses `app-server`; socket connections use `app-server-N` and `connectionIdCanResume: false`. | These are different from cloud listener IDs beginning with `conn-`. |
| Interrupted-work persistence | `src/websocket/listener/interrupted-turn-record.ts`, `recordListenerWork`: returns before persistence unless `runtime.listener.connectionId` starts with `conn-`. | The normal local App Server path does not write these interrupted-turn checkpoints. A valid input disposition does not imply that an interrupted-work record exists. |
| Automatic recorded-turn recovery | `src/websocket/listener/recover-recorded-turn.ts`, `recoverRecordedTurns`: returns unless the listener ID starts with `conn-`. | Its recorded-turn recovery loop does not supply recovery for the normal local App Server identity. |
| Backend run persistence | `src/backend/local/local-backend.ts`: `LocalBackend` extends `HeadlessBackend`. `src/backend/dev/fake-headless-backend.ts`: `runs`, active runs, controllers and replay chunks are maps; `retrieveRun` only reads its map. | The local conversation persists, but this run registry does not survive a backend process restart. The module's historical `fake` filename does not make this inheritance a fixture-only path. |
| Recovery claims | `src/websocket/listener/recovery-ownership.ts`: acquisition needs a `conn-` identity and connection generation; the default request uses `/v1/agents/{id}/recovery-claims` through the API request layer. `src/backend/api/request.ts` and `src/backend/api/agents.ts` route those requests to the configured Letta API. | These cloud-style claims are not a local App Server durable ownership mechanism. Changing only the ID prefix would not implement safe local ownership. |
| Local sync eligibility | `getRecoveryEligibility` returns `owned` for a non-`conn-` identity without the cloud runtime-status lookup; `startRecoveredApprovalContinuation` requires a claim only for `conn-` identities. | An internal `owned` label on this path is not evidence of a durable exclusive local recovery lease. |
| Stream lookup | `src/websocket/listener/recorded-run-resolution.ts`, `resolveRecordedRunId` may call `streamConversationMessages` with `otid` and `starting_after`. The local `HeadlessBackend.streamConversationMessages` calls `executeConversationTurn`, which starts a run and executes the provider. | That backend method cannot be treated as a read-only local status probe. This was established by source inspection; the dangerous call was not executed against production or the probe. |
| Attaching and syncing | `src/websocket/listener/commands/runtime-start.ts` may replay state and independently schedule a nonempty queue; `src/websocket/listener/sync-replay.ts` may start approval continuation. | Neither attachment nor sync is an evidence-only read or an exact-input, revision-conditional resume operation. |

## Why additional journal fields alone cannot authorize resume

The SDK's cloud listener recovery contract is more than a set of matching IDs:

- `createRecoveryAuthorityGuard` acquires the main record's durable filesystem
  lock and compares the current revision under that lock before executing its
  guarded action. A read from the controller cannot reserve that authority.
- With an independent successor, `createRecoveryLineageSidecarAccess` selects a
  separate recovery view and revision token. It retains exact settled results
  across later snapshots and records retired authority. A newer main record is
  not automatically the predecessor's recovery evidence.
- Input ownership includes predecessor input identities and, where applicable,
  identities from sidecars. An unreadable main or lineage can quarantine a
  scope; absence of a usable record is not permission to replay it.
- Terminal publication checks revision, lineage, terminal identity, preparation
  sequence and competing publication claims. Required and acknowledged consumers
  govern delivery. A matching terminal or an unacknowledged consumer is not by
  itself a valid current publication authority.
- Input dispositions have expiry and quarantine/retention rules. `started` and
  `replayCompleted` are not proofs of a complete model turn, a settled provider
  charge, or a completed external operation. A missing/expired disposition
  cannot prove that the input was never executed.

Andrew's current metadata reader intentionally does not acquire these locks,
create or retire sidecars, claim publication, acknowledge terminals, or certify
`authority_verified`. Extending its display would improve investigation but
would not fill the local backend's missing recovery contract.

## Isolated crash probe

A throwaway probe ran the actual pinned local App Server with temporary HOME,
settings, backend storage and controller SQLite, plus a loopback fake provider.
It submitted one input with fixed fixture input/consumer IDs. The fake provider
returned a `recent_activity` external-tool call. Once the request reached the
client, the probe recorded a fixture-only local effect and killed its own backend
process before returning a tool result. It then restarted that same temporary
backend and used `prepare_conversation` and `conversation_messages_list` only.

Observed and asserted:

| Observation | Result |
| --- | --- |
| Input accepted and external tool requested | Yes |
| Fake provider requests | 1 |
| Fixture effect count before and after backend restart | 1 |
| Saved conversation still contains the tool-call identity after restart | Yes |
| Exact bound input disposition survives | `started` |
| Exact bound interrupted-turn journal exists before or after restart | No |
| Read-only reconnection submits a new model input | No |
| Paid provider requests or real external effects | 0 |

This reproduces a new, receipt-bound interrupted input. It is distinct from the
owner's older 2026-10-17 incident, which had no controller model-turn receipts.
The probe demonstrates that even a surviving `started` disposition and saved
tool-call identity do not supply the missing interrupted-work checkpoint.
It does not prove an external effect completed; its local effect is only a
controlled crash-boundary marker.

The throwaway script and redacted output are in this session's temporary
`/tmp/andrew-letta-recovery-investigation/` directory (`probe_local_recovery.py`
and `probe-result.json`). They are not installed tooling or a permanent test.
The pinned protocol regression was also run against the current checkout to
verify completed-turn recovery and non-attaching startup validation remain intact;
its result is recorded in `Memory update.md`.

## Recommended implementation boundary

Backend support should precede a controller resume adapter. The design to review
is a local backend recovery contract with these requirements:

1. Persist input, run and tool identities plus revisioned execution boundaries
   before provider/tool dispatch. Retain exact results and terminal evidence
   across process loss. Unknown provider outcomes and charges remain held.
2. Expose a read-only inspection operation and a separate conditional resume
   operation for one exact input/revision. Resume requires an exclusive durable
   local lease with fencing. It must not pump unrelated queued input or restore
   arbitrary approvals as a side effect of inspection.
3. Admit continuation only when backend evidence agrees with Andrew's session,
   cycle, owner-control revision, tool receipts and billing state. Pause, sleep,
   restore holds, stale revisions and unresolved effects prohibit continuation.
4. Exercise process loss before dispatch, after provider dispatch, around each
   tool's effect/result commit, around terminal persistence, during competing
   ownership and after owner pause/sleep. Confirmed work must not be executed
   again; uncertain work must not become successful by inference.

Alternatives considered: using `runtime_start`/`sync` as a recovery adapter does
not meet the scoped ownership and side-effect requirements above. Reconstructing
execution from conversation text and file snapshots cannot establish unknown
provider or external outcomes. Deferring unfinished-run recovery while adding
other destination-specific receipts remains a safe separate roadmap option.

The owner approved this architectural direction on 2026-10-18. The resulting
[written specification](superpowers/specs/2026-10-18-local-letta-durable-recovery-design.md)
was approved on 2026-10-18. The implementation plan is awaiting review. No new controller/backend resume code has been written or
deployed.
