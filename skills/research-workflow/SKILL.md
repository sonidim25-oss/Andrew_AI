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

## Persistence and interruption

Relative files written under the isolated runtime's `/workspace` are backed by the persistent projects mount in the deployed system. `/tmp` and the container's own ephemeral filesystem do not provide the same guarantee. Do not claim that a file survives a restart merely because you can read it twice in the same execution.

A fresh execution container is not evidence of a controller restart or VPS reboot. Distinguish those events. To investigate persistence, save a nonsensitive marker in `/workspace`, record its identity/hash and the event you are testing, and verify it after independently established evidence of that event. If host restart evidence is unavailable, say so. Do not reboot services, alter mounts, or change deployment configuration as an experiment through unprivileged tools.

Before voluntarily stopping an investigation, save the useful findings, unresolved doubts, artifact location/reference, and a possible next step. The journal persists in the controller database; artifact retention and backups are separate concerns. After an interruption, reread the saved state. Never replay an uncertain email, media send, provider request, signup, or other external action merely to determine whether it already happened. Use read-only verification or ask the owner when evidence is unavailable.

## Expected result

A useful, revisable research record that distinguishes observations from speculation and makes continuation possible without forcing it. Keep diary summaries shareable and evidence-based. Skills remain guidance, tools remain capabilities, and memory remains your record of preferences and experiences.
