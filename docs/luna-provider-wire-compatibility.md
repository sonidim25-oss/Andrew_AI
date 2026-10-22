# Luna provider wire compatibility

The tiny synthetic tool check passed, but the real conversation was rejected:
upstream OpenAI/Azure returned HTTP400 invalid_value because historical function
names and advertised tool names contained dots (for example python.run).
The same full 103-message history was accepted after wire-name translation.

For OpenAI model IDs only, the provider adapter deep-copies the outbound payload,
assigns deterministic collision-free valid aliases to unsupported names, rewrites
definitions/history/tool choice, and restores canonical names in responses before
the gateway records results. Arguments, call IDs, canonical controller identities,
request hashes, other models and the owner's unlimited-output policy are preserved.

A separate offline maintenance method can record a permanent invalid_tool_name
rejection. It is not an HTTP400 catch-all or a gateway route. The operator must
verify the exact saved request, contemporaneous400 activity and immutable captured
validation diagnostic; the method validates the supplied typed metadata and paused,
superseding owner authority. The evidence describes a reproduction (bounded output
and tool_choice none), not a recovered original response body. Audit rows retain
the request hash, diagnostic hash, validation rule, invalid paths and cost estimate.

The full provisional reservation is conservatively consumed as an estimate; zero
provider billing is never asserted. No unrelated reservation is touched. The proof
is explicitly nonretryable, with independent refusal in controller begin_provider,
backend execution and provider_retry transition. Exact rejection import followed
by the existing owner-yield protocol closes the cancelled turn without inference
or tool execution. Unknown errors remain held.

Tests cover full name round trips/collisions, immutable payloads, unknown alias
refusal, paused maintenance guards, evidence mismatch, conservative accounting,
permanent retry refusal, and a real-backend rejected turn followed by a fresh turn.
