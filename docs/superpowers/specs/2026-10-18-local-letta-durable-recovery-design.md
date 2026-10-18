# Durable recovery of unfinished local Letta turns

**Date:** 2026-10-18
**Status:** Written specification approved by the owner on 2026-10-18; implementation plan prepared for review.
**Scope:** The pinned local Letta backend and Andrew's autonomous-session bridge.

## Outcome and limits

Continue an interrupted autonomous model turn from a proven execution boundary,
using its original input identity and saved conversation. Preserve confirmed
provider responses and tool results without repeating their effects. When an
outcome cannot be proved, retain a durable hold and explain its category.

This does not promise recovery of an unknown provider charge, arbitrary external
effect, or an old input without recovery records. It does not enable autonomy,
wake Andrew, clear existing incidents, or change owner permissions. Recovery of
owner-message turns remains outside this increment: their delivery and input
claims retain their existing handling.

The [investigation](../../letta-unfinished-recovery-investigation.md) establishes
that the pinned local App Server lacks the required interrupted-work persistence
and exclusive recovery ownership. Its `app-server` identities must not simply be
renamed to `conn-` to bypass cloud-listener guards. Existing cloud recovery code
and legacy metadata remain separate from this local recovery contract.

## Architecture and delivery artifact

Use repository-owned extension modules plus a deterministic, exact-base patch
builder for `@letta-ai/letta-code` **0.34.13**, base `letta.js` SHA-256
`eab90b26ded2647769a00ae29ec95fea1881173da53e2d54f620fc6df085b2fe`.
The builder locates reviewed source sections, refuses missing/ambiguous anchors
or any unexpected base hash, and emits a separate patched artifact with a
manifest of its input/output hashes and contract version. Keep readable extension
sources, patch inputs and tests in this repository. Do not edit the installed
npm package in place or depend on temporary extraction files.

The modified artifact runs as `andrew-letta`, with existing filesystem and network
restrictions. A root-owned service override selects the verified artifact. Its
recovery state lives under the existing writable Letta HOME, in a dedicated
`local-recovery-v1` directory; the controller never reads those private payloads
directly. This introduces no cloud recovery API or new externally exposed server.

Four components have distinct responsibilities:

| Component | Responsibility |
| --- | --- |
| Local backend recovery store | Durable input/run/step/tool records, exact payload references, revisioned transitions, transcript projection and exclusive ownership. |
| Local App Server protocol extension | Capability negotiation, evidence-only inspection, conditional admission/resume, scoped event delivery and acknowledgement. |
| Controller recovery coordinator | Match backend evidence to the saved cycle, model receipt, owner-control revision, tool evidence and billing records; admit or hold one continuation. |
| Provider/tool dispatch gates | Enforce current authorization immediately before each effect; bind outcomes to exact admissions. |

Normal turns for the new controller use the same durable execution boundaries as
recovered turns. Once a conversation adopts this contract, legacy effectful
commands cannot execute work in it outside these admissions; normal attachment
must either suppress queue/approval execution or reject the operation. Read-only
legacy conversation access remains available. Recovery cannot operate on a second execution path that bypasses
the journal. Unsupported or disabled recovery leaves existing startup behavior
intact, including conversation validation without `runtime_start` or `sync`.

## Durable identity and data model

All records bind a backend-store UUID, agent/conversation IDs, controller
session/cycle IDs, original client-message ID, terminal-consumer ID, request
content digest and owner-control revision. Backend run, provider-step and tool
attempt IDs are collision-resistant random IDs, never a process-local counter
that can be reused after restart. A continuation retains the logical input ID;
new provider steps receive new step IDs and charges, distinct from old steps.

A per-conversation authoritative checkpoint contains:

- Schema/contract version, store identity, monotonically increasing revision and
  fencing generation; execution and lease-owner identities.
- The bound input identities and a digest/reference to the accepted structured
  input. Transport acceptance follows the durable input commit.
- Ordered provider steps, each with a request digest, dispatch phase, controller
  reservation identity, and confirmed response/usage references when available.
- Tool-call ID, name, argument digest/reference, distinct execution attempt ID,
  admission state and exact returned-result reference for each controller tool.
- Typed continuation messages, transcript operation identities and the last
  confirmed step boundary. These are execution data, not reconstructed prose.
- Terminal identity/reason, original consumers, durable delivery state and
  acknowledgements. Terminal completion is distinct from Telegram delivery.

Raw input and tool-result content belongs only in private backend payload blobs,
not diagnostic reports, receipt metadata or operational logs. Do not copy auth
headers, API keys or private reasoning into the recovery journal. Preserve only
the structured output needed by the normal executor and advertised tool results.

## Persistence and transcript projection

Use the pinned SDK's durable filesystem locking primitive with a new local
namespace, not its cloud recovery claims. Serialize each conversation's state
transitions under that lock. Write immutable content-addressed payload blobs
first, flush them and their directory, then commit the authoritative checkpoint
with temporary-file write, file fsync, atomic replacement and directory fsync.
A checkpoint never references an uncommitted blob. Interrupted temporary files
are not evidence; no effect follows a failed durable commit.

Default limits are 1 MiB checkpoint metadata, 16 MiB per payload blob, and
256 MiB retained recovery data per conversation. Enforce bounds before new
effects. If a post-effect result cannot be stored, leave its admitted attempt
uncertain and hold; never repeat it to obtain a smaller result. Files are 0600,
directories 0700; reject symlinks, nonregular files, invalid IDs, malformed schemas
and digest mismatches.

The existing transcript remains available to ordinary conversation reads. For
new recovery-enabled turns, transcript entries carry stable projection operation
IDs. The recovery checkpoint and blobs are canonical for their execution data;
transcript updates are an idempotent projection with an atomic replace/append
transaction boundary that retains those operation IDs. A crash after transcript
write but before recording its completion must be resolvable by exact operation
identity and digest, without a second append. A crash before transcript write
may repair the projection from committed data without a model/provider request.

Do not rewrite legacy history, invent prior operation IDs, or infer execution
from its content. Compaction and memory maintenance triggered during a turn must
also use journaled provider-step boundaries. Compaction cannot delete the payloads
needed for an unfinished continuation. A changed transcript without matching
projection identities becomes a hold, not an opportunity to reconstruct state.

## Recovery states and effect boundaries

| Durable state | Meaning after process loss | Permitted continuation |
| --- | --- | --- |
| `input_committed` | Accepted input; no provider/tool admission exists. | Start its first step only after current authorization and exclusive claim. |
| `provider_admitted` | Dispatch boundary committed; provider response/outcome not committed. | Hold unless exact controller gateway evidence supplies the confirmed response and settled usage. No automatic retry. |
| `provider_confirmed` | Exact structured response and accounting evidence committed. | Reuse that response; never repeat that provider step. |
| `tool_prepared` | Exact tool call saved; no tool execution admission committed. | Dispatch once after current authorization and admission. |
| `tool_admitted` | Tool may have executed; exact return not committed. | Reuse independently confirmed exact result if available; otherwise hold. |
| `tool_confirmed` | Exact result committed for this call and attempt. | Supply the saved result once in the continuation; never invoke the original tool again. |
| `continuation_ready` | Complete typed input for the next provider step is committed. | Start one new provider step under a new step admission. |
| `terminal_committed` | Bound terminal evidence is durable. | Deliver/replay that terminal to its bound consumer; do not execute another step. |
| `held` | Evidence, authorization, storage or accounting cannot establish a safe boundary. | Inspection only; explicit, separately validated reconciliation is required. |

Record admission before contacting a provider or tool. Treat a crash after
admission and before the effect as uncertain too; conservative holds are an
accepted limitation of separate systems. No absence-based inference is allowed.
A per-step `requires_approval` does not complete the entire logical model turn.

## Exclusive local ownership and fencing

A conversation has one durable execution owner at a time. Acquire/renew/release
uses the local store lock and compares the expected checkpoint revision. The
claim binds process instance, OS boot identity, connection instance, controller
admission and a monotonically increasing fence. Process IDs alone are not owner
identities. Keep an OS advisory execution lock for the lifetime of ownership;
lease expiry alone never permits takeover while that lock is held.

The lease lasts 15 seconds, renews every 5 seconds and uses a monotonic deadline
within the same boot. A boot change, failed renewal, lost connection or stale
fence revokes permission to begin another effect. A replacement process must
obtain the execution lock and commit a higher fencing generation before acting.
An effect already admitted when ownership is lost is uncertain until its exact
outcome is reconciled. Fencing cannot recall a remote request already dispatched.

All active conversations, including ordinary inputs, respect the same execution
lock. Starting a second process against that store cannot bypass it through an
old protocol command. Store rollback/restore is a reconciliation hold, not a
valid lease takeover. Checkpoint revisions and fences never reset silently.

## Protocol contract

Advertise `local_durable_recovery_v1` only when the verified extension and store
are active. Every operation validates complete scope and contract version;
unknown fields, bad identities and mismatched binding fail closed. Inspection
reads a committed atomic checkpoint and reports its revision without claiming
that the revision remains current; claim/resume revalidate it under the lock.
Claim responses carry the new revision expected by the next mutation. Use these
explicit operations rather than overloading runtime attachment:

| Operation | Contract |
| --- | --- |
| `recovery_inspect` | Read one existing committed checkpoint for exact input/scope. Return revision, phase, fence state, evidence digests and fixed hold categories. Never create a runtime, lock file, lease, queue admission, provider request, terminal acknowledgement, or repaired projection. Missing/corrupt/unsupported data is reported explicitly. |
| `recovery_claim` | Compare expected revision and binding; acquire exclusive execution ownership and commit the claim. Accept one controller admission ID. No execution yet. |
| `recovery_resume` | Compare claim, fence, admission, expected revision and input digest; atomically commit a resume intent and return its attempt identity before execution. The attempt identity is idempotent: a repeated request returns its status without starting another continuation. |
| `recovery_status` | Evidence-only status of a previously admitted resume attempt; resolves a lost transport reply without re-admitting work. |
| `recovery_subscribe` | Bind the admitted controller consumer to events for this one input. No runtime attachment, queued-input pump, approval restoration or execution. |
| `recovery_result_read` | Return an exact confirmed structured result only to the bound controller for reconciliation, with its digest. This is private recovery data, never general diagnostics. |
| `recovery_revoke` | Revoke an exact claim/attempt and prevent further admissions. Preserve already admitted effects and uncertainty. |

Renewal/release belong to the exact claim, use the same fence and perform no
execution. Replaying a terminal requires the matching consumer and explicit
acknowledgement; an old terminal cannot complete a newer input. For this extension, service startup and the initial socket handshake also
suppress eager recovery, terminal promotion/cleanup and queue pumping before
any inspection command arrives. Startup does not automatically resume, consume
unrelated queued messages, or issue approvals. Recovery-owned and legacy
unresolved inputs remain fenced from ordinary queue restoration.

## Controller, gateway and tool integration

Add an independent controller recovery-attempt ledger rather than altering the
meaning of existing model-turn receipts. Bind each attempt to the saved cycle,
backend store UUID, exact input, checkpoint revision, owner revision, fence and
unique admission ID. Keep old receipts and historical holds immutable.

Before claiming or resuming, require current session/cycle identity, enabled
autonomy, unchanged owner-control revision, compatible model/tool configuration,
no pause/sleep/restore hold, no conflicting work, and no pending owner input.
Unresolved provider reservations or external actions prevent continuation. Do
not clear `autonomy_error` generally: release only the specific new interrupted
hold whose exact recorded prerequisites have been satisfied.

Use the existing serialized model worker for recovery and exclude normal session
dispatch while its attempt is active. Reuse the saved conversation. Persist the
backend admission acknowledgement before treating the attempt as executing. A
lost claim/resume reply is inspected by its original attempt identity; never
submit a fresh model input or allocate a replacement request ID to escape it.

The provider gateway must accept recovery binding metadata over its trusted
local transport and strip it before forwarding to the provider. Bind each new
provider step to a controller admission and reservation, and commit its exact
validated response/usage privately, in the same controller SQLite transaction
that settles that step's reservation, before returning success to the backend.
Keep retry/status queries separate from the provider completion endpoint. This
closes the gateway-success/backend-crash gap when exact evidence is available;
a timeout without that evidence still retains its reservation and hold.

Every tool callback carries stable backend tool-call and attempt identities.
The controller durably admits that exact call and records its exact serializable
result before returning it. A repeated callback can only return an already
confirmed matching result; an admitted call without a result cannot execute
again. Destination receipts that establish an effect but cannot establish its
exact return do not justify manufacturing a return value.

Provider and tool gates validate a controller-issued, opaque, scoped admission
against current controller state immediately before each new dispatch. Tokens
are never supplied to the model or external provider. Owner controls revoke
that scope and prevent subsequent steps even if a delayed backend resume packet
arrives after pause, sleep, or an owner revision change. Already admitted effects
retain their actual outcome; cancellation does not declare them unexecuted.

Terminal recovery follows the existing bound completion-receipt path only after
all linked tool results and billing outcomes are settled. It may finish a proven
completed turn without making a new model call. It cannot acknowledge Telegram
messages, release unrelated reservations, clear held owner messages, or complete
an unfinished turn using an older assistant answer.

## Migration, retention and restore

Recovery is opt-in for newly admitted autonomous inputs after capability
negotiation. Existing conversations remain readable, and previously deployed
text/media/Gmail receipt recovery continues to work. Legacy inputs, missing
checkpoints and mismatched store identities remain held without fabricated proof.

Collect payload blobs only after a terminal is durable, consumers are
acknowledged and controller accounting is settled. Retain identity/digest/fence
and admission tombstones until an explicit store epoch retirement; reaching the
quota causes a hold, not silent expiry of deduplication authority. Never garbage
collect unresolved evidence or create a missing result from a transcript.

Controller-only SQLite backups do not constitute backend recovery backups.
A coordinated backend/controller snapshot requires both workers quiescent and
records both store identities and snapshot digests. Restoring either side
requires explicit reconciliation before any claim or dispatch. Existing legacy
backup restore holds remain effective.

## Build, activation and rollback

Prepare and test the exact patched artifact, controller candidate and deployment
manifest before changing service selection. Installation verifies the base and
candidate hashes, capabilities, owner/group/modes, runtime constraints and all
acceptance results. Keep the previous binary/configuration and state snapshot.
Unknown deployed modifications or a contract mismatch stop installation before
services change.

Deploy backend support with automatic resume disabled first; confirm negotiated
capabilities and evidence-only inspection without restarting work. Enable the
controller recovery path only after its acceptance checks pass. Deployment must
preserve saved owner pause/sleep and queued/held work. Do not combine activation
with `/wake`, `/replay`, sample generation or a new model request.

Rollback disables automatic recovery and restores the previous service artifact
without deleting the new journals or downgrading schemas. Inputs admitted under
the new contract stay held if the older runtime cannot interpret them. Before
restarting an older backend, the rollback procedure must prevent it from
restoring or pumping these inputs, using an isolated retained snapshot and a
verified startup fence. If that exclusion cannot be verified, leave services
stopped. Reverting code is not permission to replay an admitted input.

## Acceptance evidence

All checks use isolated storage, local fake providers and fake destinations.
No paid call or real message/publication is required.

1. The exact pinned builder output advertises the contract; changed base hashes,
   ambiguous patch anchors and unsupported versions are rejected.
2. After process kill, inspection preserves input/run identities and performs
   zero recovery/controller state writes, admissions, lock-file creations,
   provider/tool calls or queue
   processing, including with missing/corrupt records and no matching input.
3. Kill before and after each durable admission, provider response commit, tool
   effect/result commit, transcript projection and terminal commit. Reopening
   either process yields the state table above and never repeats a confirmed
   provider step, tool invocation or transcript operation.
4. A gateway-confirmed response can close its backend commit gap without another
   provider call; an unknown provider outcome retains its original reservation.
   Include automatic compaction and maintenance requests in these checks.
5. Repeated resume requests and lost transport replies produce one continuation;
   status/subscription never admit another turn or consume unrelated queued work.
6. Competing backend processes and controllers cannot own the same conversation;
   renewal failure, boot change, connection loss, stale revision and fence reuse
   prevent new effects. Delayed old-worker results cannot overwrite a successor.
7. Pause/sleep/owner-input/restore races between inspection, claim, resume and each
   dispatch preserve controls and block the next effect. Test both controller
   and backend process loss independently and together.
8. Exact tool returns can be supplied after a crash without reexecution; missing,
   changed or merely destination-confirmed effects without exact results stay
   held. Old run IDs, consumers and terminals cannot settle a newer turn.
9. Legacy inputs, mismatched configuration, payload limits, full storage, missing
   blobs, invalid schemas and changed transcripts fail closed without fabricated
   checkpoints, releases or external retries.
10. Existing core, Letta protocol, installer and receipt checks pass on the source
    and merged deployment candidate. Report any unrelated baseline failures
    separately; they cannot be represented as passing acceptance.
11. A coordinated snapshot/restore and rollback with outstanding admissions keep
    those inputs held and do not trigger old-runtime queue replay.

The first useful recovered case must demonstrate a saved confirmed provider
response and tool result followed by backend/controller process loss, then one
new continuation step with the original input identity. The companion unknown
outcome case must remain held. Neither a happy-path restart nor a terminal-only
receipt satisfies unfinished-turn recovery acceptance.

## Specification review

The design defines one local ownership mechanism and one canonical execution
checkpoint, separates inspection from execution, and treats cross-system gaps as
uncertain until exact evidence closes them. Transcript repair, compaction,
owner-control races, deployment rollback and coordinated restore are included
because they otherwise permit accidental duplication. It preserves the approved
boundary without claiming recovery of inherently unverifiable outcomes.

Implementation planning and runtime changes follow review of this written
specification. The investigation probe remains throwaway evidence, not an
installed component or permanent recovery adapter.
