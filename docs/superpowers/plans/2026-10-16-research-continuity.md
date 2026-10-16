# Andrew's research continuity

Source: [Big memory update.md](../../../Big%20memory%20update.md).

## Intent

Give Andrew a durable, editable place for his own questions and unfinished investigations. Restore that context at wake and provide an optional question → investigation → observation → further-question mechanism. Do not assign interests, require research, impose waking hours, add rewards, or require explanations for sleep. Preserve owner priority, explicit sleep, error handling, and all spending/runtime limits.

## Implementation order

1. **Research journal.** Add additive SQLite tables and a validated local service. Each entry has a question, findings, doubts, a possible next step, open/paused/closed state, optional parent question and source URLs. Provide create, revision-checked update, read/list, explicit focus and delete tools. Deleting an entry removes its saved content and checkpoints; audit metadata contains no journal text. Existing memories and the owner letter remain separate.
2. **Context restoration.** Add a bounded summary of open/paused entries to autonomous turns, identify omitted entries and expose pagination. Restore saved questions as data, not instructions. Show available tools and retain the option to ignore old entries, choose another interest or sleep.
3. **Optional research cycle.** Connect related questions, keep findings distinct from doubts, and record explicit selection/resumption through a focus tool. No mandatory sequence or model call to manufacture a continuation. Optional stopping notes may explain a decision; missing notes never block sleep.
4. **Observation.** Persist structured events for journal changes, explicit topic selection/resumption, controller tool outcomes, autonomous cycles, voluntary sleep, owner controls, limits and failures. Save the selected question's existing next step at stopping points without inventing an intention. Label self-reported findings and test-provider behavior accurately.
5. **Verification and delivery.** Test restart and encrypted-backup continuity, editing/deletion and stale revisions, context bounds/pagination, safe text handling, owner control races, optional sleep notes, and several independent wakes without owner messages. Exercise actual pinned Letta callbacks with an isolated deterministic provider. Update the deployment manifest/installer for every changed module and document source readiness separately from live installation.

## Defaults and acceptance criteria

- No owner-assigned or prefilled research topics; the journal starts empty.
- Sleep remains available without a journal entry or a justification. Existing daily diary policy remains unchanged in this change.
- Source URLs and findings are Andrew-authored records, not proof of successful browsing or factual verification. Tool events establish invocation/outcome only.
- Local deterministic tests establish that the mechanism works. They do not establish spontaneous interests, curiosity or long-term autonomous behavior in production.
- Do not wake the production agent, replay uncertain messages, spend on live experiments, install, commit or push as part of this implementation request.

## Progress

- [x] Editable journal, validation, parent links, revision checks and deletion.
- [x] Bounded unfinished context restored on autonomous turns, including prior focus and stop context.
- [x] Optional focus/continuation tools and optional sleep notes, with no required research or justification.
- [x] Structured observations, stopping checkpoints and local owner inspection commands.
- [x] Persistence/backup, owner-control races, multiple independent fixture wakes, pinned Letta callbacks and deployment manifest verification.

Validation: 249 core tests run, 247 passed and two optional tests skipped. All 47 script tests passed with the pinned Letta fixture enabled. No production wake, paid model request, live installation, commit or push was performed. A natural multi-wake observation remains a post-installation assessment, not an implementation test result.
