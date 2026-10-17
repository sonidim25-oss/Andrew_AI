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

This is not general recovery of an interrupted tool call. It does not establish
whether an email, post, arbitrary Python/shell action, or provider request with an
unknown outcome completed. It does not automatically retry those actions, release
billing reservations, replay held messages, or attempt provider reconciliation.
Those cases still require an outcome receipt or service-specific idempotency and
reconciliation before recovery can be broadened. The recovery prompt is guidance;
it is not an external service's idempotency guarantee.

The next roadmap step is searchable original session history. Trust provenance
must precede automatic memory consolidation. Existing research checkpoints and
the trusted skill catalog should be extended after inspecting their current
contracts, rather than creating competing storage or skill loaders.

Validation covers real subprocess termination with `os._exit`, reopened SQLite
databases, all three checkpoint phases, legacy records, ordered transitions,
repeated recovery, owner controls, paid attempts, delivery uncertainty, and
preservation of held messages. These fixtures make no paid model calls or external
deliveries.
