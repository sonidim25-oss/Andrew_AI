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
| `model_started` | Hold the session as interrupted. The controller cannot prove what the model or tools completed. |
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
