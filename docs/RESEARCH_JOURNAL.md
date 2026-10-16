# Research continuity

Implemented in source from [Big memory update.md](../Big%20memory%20update.md). Production installation and natural autonomous behavior have not been verified for this change.

## Andrew's journal

The journal starts empty. Andrew can keep his own questions, findings, doubts, possible next steps and source URLs. Entries may be open, paused or closed; a further question can link to an earlier one. None of these fields assigns an owner task or requires research. Findings and URLs are self-reported records, not independent proof of a discovery or a successful visit.

Local tools:

| Tool | Behavior |
| --- | --- |
| `research.list` | List unfinished questions, or filter by state; follow `next_after` with `after`. Question previews are shortened. |
| `research.get` | Read the full entry, revision and latest stopping checkpoint. |
| `research.save` | Create a question or update selected fields using its ID and current revision. Omitted fields are preserved. |
| `research.focus` | Explicitly select an open/paused question; a previously selected question records resumption. |
| `research.delete` | Delete an entry using its current revision. Delete associated checkpoints and clear child parent links; child questions survive. |
| `research.observations` | Read paginated controller events for choices, tools, cycles and stopping points. |

Updates and deletion reject stale revisions, including concurrent edits. Closed entries leave focus and can be reopened. Parent links cannot form a cycle. Mutations respect owner pause, sleep and the session error state even if a command arrives between the initial tool check and the database write. The autobiographical memory, personality history and immutable owner letter remain separate.

Text uses the existing sensitive-material validation. An entry is limited to 500 question characters, 2,000 findings characters, 1,000 doubt characters, 1,000 next-step characters, five HTTP(S) source URLs and 20,000 bytes overall. The live journal holds at most 500 entries. No tool arguments, credentials or exception messages are copied to observation events.

## Wake and stop behavior

Each autonomous turn restores a bounded summary of unfinished entries, prioritizing the previously selected question without selecting it for Andrew. Context is at most 12,000 UTF-8 bytes. The summary identifies omitted/shortened data and tools to retrieve the rest. Stored text is explicitly labeled as records, not instructions. Andrew may ignore previous questions, choose another interest, reflect or sleep; the optional research cycle has no reward, required duration or completion target.

Explicit sleep, owner controls, failures and session limits preserve the selected entry's existing next step in a checkpoint. Optional `reason` on `sleep`/`wake_at` saves a brief stopping note. Missing notes and an empty journal never block sleep. No extra model call invents a next step or explains a technical failure. The existing daily diary policy and conversation quiet window remain in force.

Events distinguish explicit topic selection/resumption, journal changes, controller tool returns/errors, autonomous cycles, voluntary sleep, owner sleep/pause/wake, time/cycle limits and technical failure. Tool events retain their originating session and cycle even when an owner command interrupts the tool. A returned tool call is recorded as `returned`, not declared successful research. Empty checkpoints represent no selected investigation, not an inferred intention.

Deletion removes live journal rows and linked checkpoints. Metadata events retain IDs and fixed categories, without journal content. Existing encrypted backups, Letta conversation history and separately saved autobiographical records may retain earlier text; deleting a journal entry is not global erasure.

## Owner inspection

- `/research` lists unfinished questions without a model call.
- `/research <id>` reads an entry and its most recent stop context.
- `/research events` shows recent structured controller observations.

For natural behavior assessment, observe several independently scheduled wakes without supplying new topics. Compare explicit choices and continuation records with browser history and artifacts: did Andrew choose a site, save a new question, revisit an old one or create something? Check whether stopping was a sleep tool, an owner command, a failure or a controller limit. These are observations to collect after installation, not claims established by the implementation or percentages in the design note.

## Persistence, verification and deployment

Research entries, metadata events and checkpoints live in the existing private controller SQLite database. The additive tables are included in existing encrypted whole-database snapshots and survive restart/restore. Older databases gain the new tables at startup without rewriting existing memories or spending records. Older code can ignore the extra tables during a code rollback; the installer does not drop them.

Tests cover CRUD and parent relationships, stale/concurrent edits, validation, bounds/pagination, owner controls, deletion, encrypted backup/restore, three simulated independent wakes and distinct failure/sleep records. The pinned Letta fixture exercises real journal tool calls before and after a backend/SQLite restart. These use isolated deterministic providers and temporary databases; they demonstrate the mechanism, not spontaneous curiosity in the live Andrew.

Local result: 249 core tests run (247 passed, two optional skips), and all 47 script tests passed with the pinned Letta fixture enabled.

The deployment installer and hash manifest now include `memory.py` and `telegram_menu.py` alongside the previous controller modules. Installation still requires the administrator's terminal, runtime acceptance/proof renewal and an idle sleeping agent. It does not request a production wake, retry uncertain messages or run paid observation experiments.
