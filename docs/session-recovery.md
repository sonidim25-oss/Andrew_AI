# Session recovery checkpoints

This implements the first P0 recovery increment from [Memory update.md](../Memory%20update.md).

The controller commits a SQLite checkpoint alongside each new autonomous cycle,
before admitting its model turn, and after the complete turn returns. Checkpoints
retain the cycle identity, owner control revision, focused research identity and
revision, latest activity ID, and timestamp. They contain no tool arguments or
credentials. The existing conversation, research records, and workspace remain
the sources of saved work; recovery does not roll any of them back.

Each controller tool invocation inside a cycle also gets a durable admission
record before execution and a completion receipt afterward. Exceptions, runner
error responses, malformed browser results, or a missing completion receipt
remain uncertain even if the model subsequently returns a normal answer. Tool
arguments, results, and exception text are not copied into this ledger.

| Last durable phase | Restart behavior |
| --- | --- |
| `prepared` | Close the unstarted cycle as stopped and retain the active session. The next scheduler pass can continue without `/wake`. |
| `model_started` | Recover only if the new bound protocol completion receipt below proves the whole turn ended; otherwise hold as interrupted. |
| `model_confirmed` | Close the cycle as complete without executing its model turn or tools again. Continue the same session on the next permitted scheduler pass. |
| No checkpoint, including legacy cycles | Hold the interrupted session. Migration does not invent a checkpoint. |

Recovery checks durable state before startup quarantines claims. A provider
reservation, claimed owner input, Telegram processing or delivery claim, diary
delivery claim, unsettled media action, conflicting running cycle, changed owner
control revision, or mismatched active cycle prevents automatic recovery. Pause,
sleep, existing errors, disabled autonomy, and restore reconciliation also block
it. Pending owner input retains priority; historical held input is left held.

Every recovered cycle gets an activity receipt and preserves its checkpoint for
inspection with `diagnose-session.py --current --brief`. Repeated startup recovery
does not repeat cycle completion or dispatch any actions. A new autonomous turn
receives the recovery receipt as data and is told to continue saved work without
repeating completed external actions.

The first increment alone does not resolve external effects. The Gmail-specific
extension below supplies destination checks for newly recorded Gmail actions;
other services and interrupted model turns still need further reconciliation.
The recovery prompt is guidance, not an external service's idempotency guarantee.

Original session history and memory provenance are implemented separately; see
[session-history.md](session-history.md) and
[memory-provenance.md](memory-provenance.md). Automatic memory consolidation
remains a later roadmap step.

Validation covers real subprocess termination with `os._exit`, reopened SQLite
databases, all three checkpoint phases, legacy records, ordered transitions,
repeated recovery, owner controls, paid attempts, delivery uncertainty, and
preservation of held messages. These fixtures make no paid model calls or external
deliveries.

## External action reconciliation: Gmail

The second P0 increment adds durable receipts for new `gmail.send`,
`gmail.draft`, and `gmail.delete` calls. Before contacting Gmail, the controller
commits an action ID, caller request ID, operation/content digest, authorization
fingerprint, and cycle/tool admission link. Send/draft receipts also retain a
random RFC Message-ID and a hash of the intended recipient/subject/plain-text
body. Trash receipts retain the exact message ID. Receipts contain neither mail
bodies nor OAuth credentials. The authorization fingerprint binds the receipt to
one grant and remains stable across access-token refresh; reconnection with a
new grant conservatively requires manual inspection of older uncertain actions.

A timeout, invalid mutation response, or local failure after dispatch leaves the
receipt uncertain. Reusing the request ID cannot resend it. A new request ID
cannot bypass an earlier uncertain Gmail mutation, including legacy records.
This correlation marker is not a Gmail idempotency key: recovery never retries
the original mutation.

The `gmail.reconcile` tool and a separate background worker perform only mailbox
GET requests. They use the existing Gmail connection and owner read permission;
no new key or automatic consent is requested. OAuth access-token refresh may be
needed. The worker skips pause, sleep, and restore-reconciliation states, checks
one receipt per pass, and waits at least 60 seconds between checks of an
unresolved receipt. It does not occupy the owner-control or model worker, and
shutdown retains storage until any in-flight lookup stops.

For sends, recovery searches Sent using the saved Message-ID, then verifies the
single matching message's ID, Sent label, Message-ID, recipients, subject, and
body hash. Draft recovery similarly verifies the matching draft. Trash recovery
checks the exact message's Trash label. Matching Sent evidence establishes a
stored sent message, not recipient delivery. Empty searches, ambiguous matches,
invalid responses, content changes, unavailable permissions, account changes,
or network errors all remain uncertain. An absence from search never authorizes
resending. Google documents Message-ID search on
[messages.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list)
and [drafts.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/list).

When an autonomous model turn returns normally but a linked Gmail receipt is
still uncertain, the session enters `reconciling` and makes no further automatic
model calls. This hold survives process restart. Verified receipts can release
that hold automatically only when its `model_confirmed` checkpoint, session
identity, and owner-control revision still match. Unfinished provider attempts,
other tool effects, conflicting cycles, active delivery claims, unsettled media,
pause/sleep, and restore reconciliation prevent release. The next normal turn
receives the confirmed action receipt as data; the original action is not
replayed. The diagnostic command includes receipt identities and states without
printing message contents, lookup markers, or authorization fingerprints:

```sh
sudo python3 /home/daniel/andrew-autonomy-media/scripts/diagnose-session.py --current --brief
```

This does not automatically recover an interrupted **model** turn. Even after
Gmail evidence is verified, `model_started` cannot establish that all model/tool
work completed; that session remains held. Owner messages in uncertain state
remain held too. Existing errors and billing reservations are not cleared.
Legacy Gmail actions without a correlation receipt, arbitrary code/browser
side effects, publications, Telegram delivery, and provider uncertainty still
need their own trustworthy reconciliation adapters. Thus P0 remains partially
complete; its Gmail-specific outcome checks are implemented, not universal
exactly-once execution.

Offline checks simulate destination success followed by timeout, abrupt process
exit, SQLite reopen, confirmed-turn continuation, pending owner input, permission
revocation, grant changes, missing/ambiguous/mismatched evidence, malformed
responses, receipt-storage failure, duplicate admission, and worker shutdown.
No test sends real mail, calls a paid model, or modifies an existing mailbox.


## Bound model-turn completion receipts

The third P0 increment closes the gap between Letta finishing a turn and the
controller saving `model_confirmed`. A final-history fetch, socket failure, or
controller crash in this gap previously looked like unfinished inference.

For new autonomous turns, the controller persists the cycle/session identity,
owner revision, Letta agent/conversation IDs, input request ID, and client message
ID **before** dispatch. The input supplies its request ID as Letta's
`terminal_consumer_id`. Matching input acceptance is recorded separately.
A completion receipt is committed immediately on a whole-turn `turn_finished`
event with `end_turn`, the matching consumer, nonempty run/terminal IDs, and
observed assistant message IDs. Stream errors, intermediate step stops,
`requires_approval`, cancellation, output exhaustion, old terminal events, and
unacknowledged inputs cannot establish completion. Receipts contain identities
and states, not prompts, assistant text, hidden reasoning, or error bodies.
When Letta supplies a durable terminal acknowledgement key, the controller
acknowledges it only after committing the receipt.

On restart, or after a failed final-text fetch in the same process, a valid
receipt can advance `model_started` to `model_confirmed`. Recovery still checks
session/conversation identity and owner revision, preserves pause/sleep/restore
states and existing errors, and requires settled provider, tool, and delivery
state before continuing. An uncertain Gmail action can enter the existing
read-only reconciliation hold. Another unsettled effect remains held. The next
turn uses the original saved Letta conversation and the controller recovery
receipt; the previous input and its tools are not replayed.

This covers a controller interruption **after proven model completion**. A
model still running, an unreceived terminal event, a partial or compacted
transcript, an idle listener, and legacy turns without these receipts remain
insufficient evidence. This increment does not poll, resume, or abort unknown
backend runs during recovery, and it does not release held owner messages or
billing reservations. Recovery of genuinely unfinished model runs remains a
separate P0 task.

The pinned local Letta protocol fixture verifies real input acceptance and
consumer-bound terminal events, injects failure into the final-history fetch,
and checks that one subsequent turn preserves the original assistant message
without repeating the completed input. Offline fixtures also cover abrupt
process exit, reopened SQLite, false terminal evidence, owner overrides, missing
receipts, uncertain provider/tools, priority owner input, and the Gmail hold.
`diagnose-session.py --current --brief` now includes model receipt identities and
states. None of these tests contact real providers or send external messages.

## Telegram text delivery receipts

The fourth P0 increment covers text replies and diary entries. Telegram's
[`sendMessage`](https://core.telegram.org/bots/api#sendmessage) returns a Message
on success. The adapter now requires a positive integer message ID and exact
chat/text confirmation; an `ok` flag or arbitrary JSON alone cannot confirm a
send. Invalid confirmations remain uncertain, with fixed diagnostics that omit
bot credentials and message bodies.

Each saved delivery receives a fresh identity, and each part receives an attempt
token committed before sending. After a valid response, a separate durable
receipt stores the delivery/attempt identity, part number, chat/message IDs,
content and transmitted-part hashes, and timestamp. It contains no text or bot
token. Only then is ordinary multipart progress advanced. The delivery identity
prevents an old receipt from settling another outbox row if SQLite reuses its
numeric ID; attempt tokens prevent late responses from settling a new attempt.

Startup reconciles these positive receipts before deciding whether a session
has an unsettled Telegram delivery. The existing recovery worker also reconciles
held text deliveries locally, without Telegram or model calls, while awake and
unpaused. Runtime reconciliation leaves active sender claims alone. Fully
confirmed deliveries leave the outbox, confirmed diaries become delivered, and
a confirmed prefix can release only its definitively unsent suffix. Explicit
diary retry consumes existing positive evidence before considering a resend.
Receipt history remains available to `diagnose-session.py --current --brief`.

This closes the local failure gap after a confirmation was durably recorded but
before progress or final outbox cleanup was saved. If Telegram accepted a send
but its response was lost, or receipt storage itself failed, the outcome stays
held. The Bot API provides no general bot-sent-history lookup or sendMessage
idempotency key that this adapter can use to resolve that gap. It makes no probe
sends, edits, forwards, deletions, or blind retries. A confirmed send is not a
read receipt. Media delivery and legacy attempts without positive receipts still
need separate recovery work. Existing session errors, held owner inputs, pause,
sleep, and provider reservations are preserved.

Offline tests cover abrupt process exit after receipt commit, SQLite reopen,
progress/finalization failures, Unicode multipart delivery, confirmed prefixes
followed by unknown parts, reused row IDs, changed chats/content, stale attempts,
malformed confirmations, explicit diary retries, active sender ownership, and
receipt-storage failures. Simulated network timeouts stay held with no second
send. Test senders return realistic Message confirmations; tests do not contact
Telegram or a paid model.

## Startup does not resume backend work

Startup validation now uses `conversation_retrieve` for an existing saved
conversation. Only an installation without a conversation ID uses
`conversation_create`. Both responses must identify the expected agent and
conversation; an unavailable or mismatched saved conversation fails validation
without silently creating a replacement. These management commands do not
attach a runtime, submit input, recover approvals, or acknowledge tool work.
Normal runtime attachment remains in the controller's admitted turn path.
Validation still registers the provider and updates agent configuration, as
before; it is not a completely read-only setup operation.

Previously `LettaAgent.initialize()` called `runtime_start` with
`recover_approvals: true` before `SessionRunner.recover()`. That was unsuitable
for examining unfinished work: attachment itself can start recovery or pump
already queued inputs, even without a new controller input. Disabling only
`recover_approvals` would not disable every queue/continuation path. The change
also applies to validation-only installer checks and preserves pause, sleep,
errors, held inputs and billing reservations.

The investigation used the pinned local `@letta-ai/letta-code` **0.34.13**
bundle (`letta.js` SHA-256
`eab90b26ded2647769a00ae29ec95fea1881173da53e2d54f620fc6df085b2fe`):

- `HeadlessBackend` stores runs, active run identities and replay chunks in
  in-process maps. Its `retrieveRun` cannot recover that state after backend
  process loss.
- `readMessageStatus` requires Cloud environment routing; it is not a local
  per-input completion lookup.
- `buildLoopStatus` may expose `WAITING_ON_INPUT` with empty active run IDs for
  an interrupted cache. Idle is therefore not proof of completion.
- `handleRuntimeStartProtocolCommand` and `replaySyncStateForRuntime` contain
  recovery/queue execution paths. Neither is used as a diagnostic read here.
- The SDK has durable interrupted-input and terminal journals, but they have
  ownership, consumer and authority-revision rules. This change does not treat
  a loose journal file, assistant transcript or missing run as completion proof.

Genuinely unfinished turns remain held unless a separate, reviewed recovery
adapter can establish their outcome. This is a startup execution fix, not a
claim that P0 unfinished-run recovery is complete. The pinned protocol regression
keeps a real provider run unfinished while initializing a replacement controller
twice, verifies no runtime attachment/input/abort/tool response and no extra
provider request, preserves owner controls and queued inputs, and then lets only
the original run complete. All provider responses are local fixtures.

## Read-only administrator journal metadata

The optional diagnostic reads local recovery files directly, without importing
Letta, connecting to its socket, obtaining SDK locks, acknowledging terminals,
pruning journals, or modifying the controller database:

```bash
sudo python3 /home/daniel/andrew-autonomy-media/scripts/diagnose-session.py --current --brief --letta-recovery-metadata
```

`--letta-home` defaults to the Letta service's `/var/lib/andrew/letta`. The
command examines only exact scope filenames under `.letta/listener-state` and
`.letta/listener-terminal-state`, and the v4 input-disposition ledger under
`.letta/state`. It compares saved controller client-message and consumer IDs,
reports tool/result counts and fixed state labels, and never prints tool results,
queued input contents, credentials, working directories or authority values.
Namespace names are represented by hashes rather than printed server URLs.

Reads reject symlinks in all path components and non-regular files, cap each
file at 1 MiB, reject duplicate JSON keys, and report changes during a read.
The scan is bounded to five controller receipts and 16 namespaces per kind;
missing/unreadable/oversized/unsupported records and limits are explicit.
Missing files are not evidence that an input was never admitted or that a run
completed. Multiple files are not an atomic snapshot. The parser describes the
pinned 0.34.13 format; its output does not attest the installed SDK version or
validate every field in the SDK's schema.

This is metadata for investigating recovery, not a new recovery adapter.
`replayCompleted` is the SDK ledger marker, not proof that an external action
succeeded. `authority_verified` and `automatic_resume` always remain false,
including for matching `end_turn` terminals. Consumer ownership, lineage
sidecars, authority revisions, expiry and action/billing outcomes still require
separate verification before any automatic continuation.

The pinned protocol fixture inspects the real input-disposition ledger before
the controller sends its terminal acknowledgement. Normal completion can be
recorded there while the separate terminal journal is empty. Offline tests also
verify private-data redaction, identity mismatches, malformed records,
symlink/FIFO handling, scan/file limits and an unchanged controller database.
No service reinstall is needed to run this administrator script from the checkout.
