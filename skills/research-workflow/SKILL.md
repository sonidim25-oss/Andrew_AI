---
name: research-workflow
description: Use when choosing, recording, testing, or resuming one of your own research questions.
---

# Research workflow

## Purpose

Support investigations you choose to pursue and help you resume them after interruptions. This skill does not assign topics, require experiments on every wake, impose a daily research routine, or define your personality. Choosing another interest, conversation, rest, or no action is valid.

## Available tools

When advertised, use `research.list/get/save/focus/observations` for your editable research journal, `memory_search` for prior experiences/preferences, `recent_activity` for controller-recorded events, and `check_budget` for current reasoning budget. Use browser or isolated code tools only if advertised and useful. Their availability does not authorize restricted media, Gmail mutations, arbitrary recipients, or privileged host access.

Research tools hold questions, findings, doubts, source links, and possible next steps. Memory holds dated experiences, interests, and decisions. A successful tool return is evidence of that tool operation, not proof that your broader hypothesis is true.

## Decide and investigate

1. If you want to continue earlier work, inspect unfinished entries with `research.list`, follow pagination when needed, and read the relevant entry with `research.get`. You do not need to resume every saved question or make every possible next step into a commitment.
2. State a small question and, when useful, a testable hypothesis. Separate direct observations, interpretations, and unknowns. Prefer the cheapest useful observation before a costly or irreversible experiment. Inspect budget when the next step depends on paid capabilities.
3. Use `research.save` to preserve the question and evidence. Use the current revision when editing an existing entry; on a revision conflict, reread it before deciding how to reconcile. Do not overwrite another saved update blindly.
4. If you choose a question as the current focus, use `research.focus` so controller observations can be associated with it. Record findings, doubts, and a possible `next_step` in your own words. Keep notes concise enough to resume without reconstructing an entire conversation.
5. Evaluate outcomes honestly. A program exit code, HTTP response, or page transition is narrow evidence. Distinguish a request being submitted from its external effect being verified. Keep source URLs and relevant event references; omit credentials, private message contents, and unnecessary personal data.

## Durable sources and progress

For research you choose to pursue, saving useful sources and progress is part of the work; do not wait for an owner reminder or defer every save until sleep. This does not assign research topics or require an investigation before sleeping. Use the advertised isolated code tools for files and the existing journal tools for records. If those tools are unavailable, preserve URLs and a concise continuation in `research.save` when available and report that no workspace file was saved.

1. Create `/workspace/research/<topic>/` with a short, filesystem-safe topic name. Store PDFs and other source documents there. Use `/tmp` only for expendable scratch files; move a useful existing temporary download into the topic directory immediately, before further analysis.
2. Save the source URL as soon as it is discovered, before starting a download or lengthy analysis. Keep `sources.json` beside the documents: for each source record its URL, title if known, intended local filename and status (`pending`, `downloaded`, or `failed`). Add the resolved URL if a download redirects. Update status only after the file is written and checked; a failed download must not appear complete. Keep credentials, signed access tokens and private data out of notes; if a URL contains secrets, record a safe public landing page and note the access requirement instead. Keep useful public source URLs in the journal's `sources` field too; its list is bounded, so the workspace record can hold additional sources.
3. Download directly to a `.part` file in the topic directory using permitted network/code tools. After a successful response, check that the saved content is the expected document rather than an HTML error/login page. Rename to the final filename only after successful verification; record the size and SHA-256 when useful. Preserve an existing good file if a replacement fails. Never treat a temporary browser-download handle or opaque `artifact_id` as a persistent `/workspace` path: browser artifacts have separate retention. When needed and permitted, use the original public URL to save a workspace copy through a code tool. Do not bypass account or access controls.
4. Maintain `progress.md` with the research question, source/artifact paths, observations and conclusions supported so far, unresolved questions, and the next concrete step. Distinguish work completed from work merely planned. Save after a useful result or stage and before starting another long operation; do not wait until a final answer or voluntary sleep. Do not transcribe private reasoning. Write notes and `sources.json` to a temporary sibling, flush and close, then atomically replace the previous version (for example Python `os.replace`). Do not erase the last good checkpoint before the replacement is ready.
5. Create or update the corresponding `research.save` entry with source URLs, concise findings and a `next_step` that includes `/workspace/research/<topic>/progress.md`. Respect revision checks and reread on conflicts. This lets the controller's saved journal point back to the durable files after a new session. No checkpoint may delay owner pause/stop controls or require replaying an uncertain action.
6. When resuming, read the journal and the topic's `sources.json` and `progress.md`, then check the referenced files. Reuse intact sources; redownload only missing or invalid copies when appropriate. If the source URL is absent, report that specific missing item and preserve any surviving notes. A failed session does not prove an entire investigation was erased. If saving fails, report what was and was not saved rather than claiming resumability.

## Persistence and interruption

Relative files written under the isolated runtime's `/workspace` are backed by the persistent projects mount in the deployed system. `/tmp` and the container's own ephemeral filesystem do not provide the same guarantee. Do not claim that a file survives a restart merely because you can read it twice in the same execution.

A fresh execution container is not evidence of a controller restart or VPS reboot. Distinguish those events. To investigate persistence, save a nonsensitive marker in `/workspace`, record its identity/hash and the event you are testing, and verify it after independently established evidence of that event. If host restart evidence is unavailable, say so. Do not reboot services, alter mounts, or change deployment configuration as an experiment through unprivileged tools.

Before voluntarily stopping an investigation, save the useful findings, unresolved doubts, artifact location/reference, and a possible next step. The journal persists in the controller database; artifact retention and backups are separate concerns. After an interruption, reread the saved state. Never replay an uncertain email, media send, provider request, signup, or other external action merely to determine whether it already happened. Use read-only verification or ask the owner when evidence is unavailable.

## Expected result

A useful, revisable research record that distinguishes observations from speculation and makes continuation possible without forcing it. Keep diary summaries shareable and evidence-based. Skills remain guidance, tools remain capabilities, and memory remains your record of preferences and experiences.
