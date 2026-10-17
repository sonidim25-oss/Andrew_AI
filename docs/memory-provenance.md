# Memory provenance

This implements the remaining P1 item in [Memory update.md](../Memory%20update.md).
Memory author, evidence origin, and instruction authority are separate concepts.

| Provenance | Meaning |
| --- | --- |
| `owner` | A record written through the trusted controller/administrator owner path. |
| `agent` | Andrew-authored content without identified external evidence in this write. This is not independent verification. |
| `untrusted` | External content, linked assistant/tool claims, incomplete evidence, or an unverified legacy agent record. |
| `system` | A record written through trusted application code using the system source. |

The existing `source` author field remains intact. A separate immutable provenance
ledger records origin and classification basis, and an immutable link table keeps
references to original session-history records. `memory_search` returns these
labels and history IDs alongside its existing diary/event references. The
`history_ids` argument on the two memory-write tools links evidence returned by
`session_search` or `session_read`. References are checked against the configured
owner; another owner's history cannot be cited or inspected through this path.

The controller assigns provenance. The model cannot submit `source`, `provenance`,
`owner_id`, or a trust override. Citing an owner message does not make Andrew's new
text owner-authored. Linked assistant/tool text or incomplete history makes the
derived record untrusted. Missing references reject the write atomically, including
its FTS entry and provenance metadata.

A per-turn context also marks exposure to browser/code results, mail, research
reads, untrusted history, and untrusted memory search results. Subsequent memory
writes in that turn are untrusted even if optional history links are omitted.
The context is reset when the turn exits, including failure and cancellation.
This guard is conservative: code output may be an observation, but it is not
automatically a source of trusted behavioral instructions.

Untrusted memories remain stored and searchable. An untrusted personality proposal
is saved as a candidate, but does not replace the active personality injected by
the gateway. The latest eligible owner/agent/system revision remains active.
Every injected personality revision is explicitly labelled read-only data. No
provenance label grants permission, edits the founding letter, authorizes an
external action, or turns remembered text into a system instruction.

Migration is additive and idempotent. Existing owner/system author records retain
their known origin. Historical Andrew records get `untrusted` with the
`legacy_unverified` basis because their original evidence was not recorded.
Their text, IDs, references, FTS entries and protected owner letter are retained.
Legacy personality revisions remain searchable but cease automatic injection;
if none is eligible, there is no active personality revision from that table.
There is no automatic promotion or mass rewriting of older memories. The new
ledger and evidence links are included in full encrypted database backups.

This is provenance accounting, not semantic proof or a complete prompt-injection
defense. It cannot reliably identify an uncited paraphrase of external content
introduced in a later turn. Even an `agent` or `owner` label is not a truth score,
and all recalled content remains data. Automated consolidation and memory
promotion are deliberately not introduced here.

Tests cover forged origin fields, cross-owner references, immutable classifications
and links, rollback, malicious external suggestions, personality selection, legacy
migration with a protected owner letter, and encrypted restore. The pinned runtime
also reads untrusted archived text and writes memory without a source link,
verifying that it remains untrusted and that turn context is subsequently cleared.
