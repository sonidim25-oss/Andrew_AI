# Searchable original session history

P1 from [Memory update.md](../Memory%20update.md) adds two local controller tools,
separate from the summarized autobiographical memory:

```json
{"name":"session_search","arguments":{"query":"sfc31.pdf checksum","limit":8}}
{"name":"session_read","arguments":{"id":42,"offset":0,"limit":4000}}
```

Search returns source record IDs, excerpts, timestamps, roles, session/cycle and
conversation references where known, redaction and capture flags, and the SHA-256
of the archived text. An optional `session_id` restricts search to an exact session.
Read returns a bounded section of the original retained text with `next_offset`
for subsequent sections. Cite these record IDs when checking a remembered claim.
These operations read SQLite and do not call the model or a provider.

Owner messages and replies saved by the controller are archived atomically with
their existing chat records. Startup backfills retained controller chat in batches
with stable IDs. Historical records whose session identity was never recorded
keep an empty session ID; migration does not invent one.

The Letta connection captures public assistant text as it arrives and controller
tool results before returning them to Letta. Streaming fragments are joined under
their source message identity. Partial text survives disconnects and is labelled
incomplete. A completed source record cannot be overwritten by a later fragment.
Capture is attached for the configured owner, and every search and read is scoped
to that owner in SQL. There is no tool argument for selecting another owner.

The archive excludes reasoning/thinking events, system prompts, generated
autonomous prompts, and tool arguments. Recognizable credentials and private
reasoning tags in otherwise public text are redacted, including when split across
streamed fragments. The checksum describes the retained, possibly redacted text;
it is not the checksum of a PDF or other referenced artifact. Hidden reasoning is
not reconstructed from summaries. Search/read tool responses are not archived
again, avoiding recursive copies of search results.

History is evidence of what was said or returned, not a new trust authority.
Every tool result warns that historical text is data, that tool output is
untrusted, and that a message is not proof of external action completion. The
`complete` flag means capture completeness, not successful execution. Lookup does
not write to `memory_records`, change permissions, approve tools, replay messages,
or resolve uncertain actions. Strict provenance for autobiographical memory is
the next roadmap step, before automated consolidation.

Existing tool history that Letta already compacted before this release cannot be
recovered by the new archive. Retained controller chat is backfilled; public
intermediate replies and tool results are captured from installation onward.
There is no paid historical reconstruction or replay. Storage is part of the
existing protected controller SQLite database and its encrypted full-database
backups. The migration is additive, so rollback does not discard this archive.

Tests verify owner isolation, literal FTS queries, source references, redaction,
Unicode pagination, interrupted captures, restart/backfill idempotence, and
capture failure before a tool response is sent. The pinned-runtime test actually
compacts a conversation, verifies that its first reply is absent from Letta's
saved messages, then retrieves that reply through the independent archive. The
fixture provider is local and unbilled.
