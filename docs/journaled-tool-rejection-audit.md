# P0-0b: journaled tool rejection audit

Baseline: `bffca82`; implementation builds on P2-11 `239f451`.

`ToolRejected` is a ValueError-compatible, explicit pre-action rejection. Only
fixed, non-sensitive controller messages are allowed. Audit/ledger writes may
precede rejection; the requested action has not started. RecoveryDispatch catches
only this type from the callback, persists the exact error result using the normal
confirmation path, and reuses it on duplicate dispatch. The controller also settles
the cycle tool ledger so a confirmed rejection cannot become a second uncertainty
hold. A rejected sleep never receives a terminal-sleep marker. Unknown errors,
failed receipt persistence, authority/fence/claim validation, runner mapping errors,
and potentially post-action failures retain uncertainty.

The original estimate was about 96 plain raises. AST inspection finds **95** in
the stated scope: controller `_execute_tool` 21, memory 30, media 34, Gmail 4,
skills 6. Every site is listed below using baseline line numbers. P2-11 already
removed one duplicate controller guard. This count excludes the existing
ResearchValidationError and GmailError subclasses; GmailError handling is audited
separately below rather than treating all ValueError subclasses as safe.

## Original plain ValueError sites

| Baseline site | Check | Disposition and effect boundary |
| --- | --- | --- |
| controller.py:380 | `_execute_tool` / `ValueError('tool arguments must be an object')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:383 | `_execute_tool` / `ValueError('owner pause can only be cleared by the owner with /wake')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:385 | `_execute_tool` / `ValueError('sleep can only end at a scheduled wake or an owner wake')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:388 | `_execute_tool` / `ValueError("save today's diary before requesting autonomous sleep")` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:395 | `_execute_tool` / `ValueError('Media unavailable')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:405 | `_execute_tool` / `ValueError('memory service is unavailable')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:411 | `_execute_tool` / `ValueError('unsupported memory arguments; provenance is assigned by the controller')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:448 | `_execute_tool` / `ValueError('sleep duration must be an integer from 1 to 1440 minutes')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:460 | `_execute_tool` / `ValueError('wake time must use local HH:MM format')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:464 | `_execute_tool` / `ValueError` | Internal parse sentinel; translated by pre-effect handler |
| controller.py:467 | `_execute_tool` / `ValueError('wake time or configured timezone is invalid')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:479 | `_execute_tool` / `ValueError('A failed session requires owner recovery before autonomous activity')` | Removed in P2-11; authoritative Store guard retained |
| controller.py:485 | `_execute_tool` / `ValueError('check_budget takes no arguments')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:489 | `_execute_tool` / `ValueError('Gmail is not configured')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:493 | `_execute_tool` / `ValueError(f'Gmail action blocked by {reason} budget gate')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:498 | `_execute_tool` / `ValueError('activity limit must be from 1 to 8')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:507 | `_execute_tool` / `ValueError(f'external action blocked by {reason} budget gate')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:509 | `_execute_tool` / `ValueError('isolated runtime is unavailable')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:522 | `_execute_tool` / `ValueError('activity event references are invalid')` | Typed pre-effect rejection; only reads/audit precede action |
| controller.py:530 | `_execute_tool` / `ValueError('diary entry was not saved; check the date and event references')` | Unknown storage error; stays ValueError (may follow commit) |
| controller.py:533 | `_execute_tool` / `ValueError('unsupported controller action')` | Typed pre-effect rejection; only reads/audit precede action |
| memory.py:33 | `validate_memory_text` / `ValueError('memory text is empty or exceeds its allowed length')` | Typed pre-effect validation/read-only rejection |
| memory.py:36 | `validate_memory_text` / `ValueError('memory text contains disallowed sensitive material')` | Typed pre-effect validation/read-only rejection |
| memory.py:117 | `execute` / `ValueError('history arguments must be an object')` | Typed pre-effect validation/read-only rejection |
| memory.py:120 | `execute` / `ValueError('unsupported history search arguments')` | Typed pre-effect validation/read-only rejection |
| memory.py:123 | `execute` / `ValueError('history query must contain 1 to 500 characters')` | Typed pre-effect validation/read-only rejection |
| memory.py:125 | `execute` / `ValueError('history result limit must be from 1 to 20')` | Typed pre-effect validation/read-only rejection |
| memory.py:127 | `execute` / `ValueError('invalid session ID')` | Typed pre-effect validation/read-only rejection |
| memory.py:137 | `execute` / `ValueError('unsupported history read arguments')` | Typed pre-effect validation/read-only rejection |
| memory.py:141 | `execute` / `ValueError('invalid history read range')` | Typed pre-effect validation/read-only rejection |
| memory.py:144 | `execute` / `ValueError('history record not found')` | Typed pre-effect validation/read-only rejection |
| memory.py:151 | `execute` / `ValueError('unsupported history action')` | Typed pre-effect validation/read-only rejection |
| memory.py:162 | `_validate_references` / `ValueError('memory references are invalid')` | Typed pre-effect validation/read-only rejection |
| memory.py:164 | `_validate_references` / `ValueError('memory references are invalid')` | Typed pre-effect validation/read-only rejection |
| memory.py:184 | `propose_personality_edit` / `ValueError('personality edit was rejected; check its text and references')` | Unknown storage error; stays ValueError (may follow commit) |
| memory.py:190 | `add_experience` / `ValueError('invalid memory record type')` | Typed pre-effect validation/read-only rejection |
| memory.py:197 | `add_experience` / `ValueError('memory record was rejected; check its type, text, and references')` | Unknown storage error; stays ValueError (may follow commit) |
| memory.py:201 | `search` / `ValueError('memory result limit must be from 1 to 20')` | Typed pre-effect validation/read-only rejection |
| memory.py:253 | `_identifier` / `ValueError('research ID must identify a journal entry')` | Typed pre-effect validation/read-only rejection |
| memory.py:259 | `_integer` / `ValueError('research revision, cursor or limit is invalid')` | Typed pre-effect validation/read-only rejection |
| memory.py:268 | `prepare_save` / `ValueError('invalid research arguments')` | Pure save validator; retained research-specific rejection result |
| memory.py:272 | `prepare_save` / `ValueError('a new research entry requires a question; updates require at least one field')` | Pure save validator; retained research-specific rejection result |
| memory.py:274 | `prepare_save` / `ValueError('revision is only used when editing an existing entry')` | Pure save validator; retained research-specific rejection result |
| memory.py:285 | `prepare_save` / `ValueError('invalid research status')` | Pure save validator; retained research-specific rejection result |
| memory.py:291 | `prepare_save` / `ValueError('provide at most five source URLs')` | Pure save validator; retained research-specific rejection result |
| memory.py:297 | `prepare_save` / `ValueError('source URLs require HTTP(S) and cannot contain credentials')` | Pure save validator; retained research-specific rejection result |
| memory.py:315 | `execute` / `ValueError('research arguments must be an object')` | Typed pre-effect validation/read-only rejection |
| memory.py:322 | `execute` / `ValueError('unsupported research operation or fields')` | Typed pre-effect validation/read-only rejection |
| memory.py:331 | `execute` / `ValueError('invalid research status')` | Typed pre-effect validation/read-only rejection |
| memory.py:341 | `execute` / `ValueError('research entry ID is required')` | Typed pre-effect validation/read-only rejection |
| memory.py:348 | `execute` / `ValueError('research entry was not found')` | Typed pre-effect validation/read-only rejection |
| media.py:79 | `__post_init__` / `ValueError('Media budgets must be positive finite amounts')` | Startup configuration; stays ValueError, outside tool execution |
| media.py:82 | `__post_init__` / `ValueError('Select Eleven v3 and an approved premade voice')` | Startup configuration; stays ValueError, outside tool execution |
| media.py:84 | `__post_init__` / `ValueError('Unsupported media configuration')` | Startup configuration; stays ValueError, outside tool execution |
| media.py:90 | `load_media_policy` / `ValueError('Media requires verified provider capabilities before enabling')` | Startup configuration; stays ValueError, outside tool execution |
| media.py:103 | `load_media_policy` / `ValueError('Invalid media policy configuration')` | Startup configuration; stays ValueError, outside tool execution |
| media.py:161 | `submit` / `ValueError('Restore requires billing and action reconciliation')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:163 | `submit` / `ValueError('Media generation is disabled')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:165 | `submit` / `ValueError('Spontaneous generation is paused or sleeping')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:167 | `submit` / `ValueError('Media requires an owner request or standing permission')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:170 | `submit` / `ValueError('Media input is empty or too long')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:172 | `submit` / `ValueError('Invalid media request ID')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:174 | `submit` / `ValueError('Only the configured voice is approved')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:177 | `submit` / `ValueError('Invalid image edit artifact')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:191 | `submit` / `ValueError('Media request ID was already used with different arguments')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:198 | `submit` / `ValueError('Image edits require a delivered owner artifact')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:218 | `execute` / `ValueError('Invalid media request ID')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:225 | `execute` / `ValueError('This owner request does not authorize that media action')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:232 | `execute` / `ValueError('Media request ID does not match the saved request')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:236 | `execute` / `ValueError('No saved request or standing permission for this media action')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:239 | `execute` / `ValueError('Invalid media tool arguments')` | Typed pre-effect rejection; before job/proposal INSERT |
| media.py:245 | `job` / `ValueError('Unknown media job')` | Conditional: typed only at explicit submit/proposal/read preflight; worker/post-submit callers stay ValueError |
| media.py:296 | `complete` / `ValueError('Media job is not running')` | Post-generation/delivery validation; stays ValueError, uncertain outcome |
| media.py:298 | `complete` / `ValueError('Invalid media size')` | Post-generation/delivery validation; stays ValueError, uncertain outcome |
| media.py:306 | `complete` / `ValueError('Invalid media type')` | Post-generation/delivery validation; stays ValueError, uncertain outcome |
| media.py:308 | `complete` / `ValueError('Invalid media usage')` | Post-generation/delivery validation; stays ValueError, uncertain outcome |
| media.py:310 | `complete` / `ValueError('Media artifact storage quota exhausted')` | Post-generation/delivery validation; stays ValueError, uncertain outcome |
| media.py:330 | `artifact` / `ValueError('Invalid artifact reference')` | Conditional: typed only at explicit submit/proposal/read preflight; worker/post-submit callers stay ValueError |
| media.py:333 | `artifact` / `ValueError('Invalid artifact')` | Conditional: typed only at explicit submit/proposal/read preflight; worker/post-submit callers stay ValueError |
| media.py:336 | `artifact` / `ValueError('Artifact checksum mismatch')` | Conditional: typed only at explicit submit/proposal/read preflight; worker/post-submit callers stay ValueError |
| media.py:357 | `save_delivery_receipt` / `ValueError('Media delivery admission is missing')` | Post-generation/delivery validation; stays ValueError, uncertain outcome |
| media.py:363 | `save_delivery_receipt` / `ValueError('Media delivery admission changed')` | Post-generation/delivery validation; stays ValueError, uncertain outcome |
| media.py:393 | `acknowledge` / `ValueError('Media delivery confirmation is missing')` | Post-generation/delivery validation; stays ValueError, uncertain outcome |
| media.py:397 | `acknowledge` / `ValueError('Media delivery confirmation no longer matches')` | Post-generation/delivery validation; stays ValueError, uncertain outcome |
| media.py:433 | `propose_avatar` / `ValueError('An image preview must be delivered to the owner first')` | Typed pre-effect rejection; before job/proposal INSERT |
| gmail_tools.py:113 | `message_fingerprint` / `ValueError` | Evidence parsing sentinel; stays GmailError through handler, also used after send/reconciliation |
| gmail_tools.py:115 | `message_fingerprint` / `ValueError` | Evidence parsing sentinel; stays GmailError through handler, also used after send/reconciliation |
| gmail_tools.py:118 | `message_fingerprint` / `ValueError` | Evidence parsing sentinel; stays GmailError through handler, also used after send/reconciliation |
| gmail_tools.py:121 | `message_fingerprint` / `ValueError` | Evidence parsing sentinel; stays GmailError through handler, also used after send/reconciliation |
| skills.py:29 | `execute` / `ValueError('Skill arguments must be an object')` | Typed read-only skill rejection; no action |
| skills.py:34 | `execute` / `ValueError('Invalid skill request')` | Typed read-only skill rejection; no action |
| skills.py:37 | `execute` / `ValueError('Unknown skill; use skills.list')` | Typed read-only skill rejection; no action |
| skills.py:41 | `execute` / `ValueError()` | Internal read-only loader sentinel; translated to typed rejection |
| skills.py:45 | `execute` / `ValueError()` | Internal read-only loader sentinel; translated to typed rejection |
| skills.py:48 | `execute` / `ValueError('Trusted skill document unavailable or changed; report it without substituting workspace instructions')` | Typed read-only skill rejection; no action |

## Additional reachable validation

Store research save/focus and lifecycle guards are typed only at explicit checks
before mutation. Research delete now checks the requested revision before changing
children, under BEGIN IMMEDIATE; its post-write defensive failure remains unknown.
The P2-11 exact authority context and writer validation are preserved.

Memory record/personality input and event/diary/history reference checks precede
insertion. History provenance validation is split from insertion, removing its old
post-INSERT validation boundary. Unknown exceptions from storage are not promoted,
even if the normal transaction would roll back. Founding-letter provenance remains
fixed owner/controller provenance.

Diary pure text validation is translated only before calling storage. Store's
explicit date/type/reference/duplicate checks before its first INSERT are typed;
unknown exceptions during or after storage remain plain.

Shared media `job`/`artifact` helpers take an explicit preflight flag only at
read-only get, edit submission, and avatar proposal validation. Worker calls and
lookups after a successful queue INSERT use the default and retain ValueError.
Filesystem failures with unknown cause are not automatically reclassified.

## GmailError subclass audit

The four plain Gmail ValueErrors above were never ordinary argument validation.
GmailError itself inherits ValueError, so it also requires a phase audit.
`GmailRejected` inherits both ToolRejected and GmailError for legacy compatibility.
Only composition checks, execute argument/permission/scope checks, read argument
checks before its first request, and write argument checks before claiming/sending
use it. The following original GmailError sites are classified individually:

| Baseline site | Check | Disposition |
| --- | --- | --- |
| gmail_tools.py:91 | `compose_message` / `GmailError('Supply 1 to 10 plain recipient email addresses')` | Typed pre-dispatch rejection |
| gmail_tools.py:96 | `compose_message` / `GmailError('Subject or plain-text body is invalid or too long')` | Typed pre-dispatch rejection |
| gmail_tools.py:109 | `message_fingerprint` / `GmailError('Message evidence is invalid')` | Retained unknown/evidence/previous-uncertainty failure; no automatic settlement |
| gmail_tools.py:126 | `message_fingerprint` / `GmailError('Message evidence is invalid')` | Retained unknown/evidence/previous-uncertainty failure; no automatic settlement |
| gmail_tools.py:151 | `execute` / `GmailError('Gmail arguments must be an object')` | Typed pre-dispatch rejection |
| gmail_tools.py:154 | `execute` / `GmailError('Gmail status takes no arguments')` | Typed pre-dispatch rejection |
| gmail_tools.py:158 | `execute` / `GmailError('Supply an existing Gmail request_id')` | Typed pre-dispatch rejection |
| gmail_tools.py:163 | `execute` / `GmailError('Unsupported Gmail action')` | Typed pre-dispatch rejection |
| gmail_tools.py:166 | `execute` / `GmailError(f'Gmail {permission} permission is disabled by the owner')` | Typed pre-dispatch rejection |
| gmail_tools.py:171 | `execute` / `GmailError(f'Gmail {permission} requires new OAuth consent in the terminal')` | Typed pre-dispatch rejection |
| gmail_tools.py:188 | `_read` / `GmailError('Invalid Gmail message ID')` | Typed pre-dispatch rejection |
| gmail_tools.py:194 | `_read` / `GmailError('Invalid Gmail search or result limit')` | Typed pre-dispatch rejection |
| gmail_tools.py:203 | `_read` / `GmailError('Google returned an invalid message ID')` | Retained unknown/evidence/previous-uncertainty failure; no automatic settlement |
| gmail_tools.py:207 | `_read` / `GmailError('Use Gmail list or get with the documented arguments')` | Typed pre-dispatch rejection |
| gmail_tools.py:212 | `_write` / `GmailError('Invalid Gmail mutation fields')` | Typed pre-dispatch rejection |
| gmail_tools.py:215 | `_write` / `GmailError('Mutation needs a unique request_id of 8 to 80 letters, digits, hyphens or underscores')` | Typed pre-dispatch rejection |
| gmail_tools.py:220 | `_write` / `GmailError('Invalid Gmail message ID')` | Typed pre-dispatch rejection |
| gmail_tools.py:232 | `_write` / `GmailError('Gmail account binding is unavailable')` | Retained unknown/evidence/previous-uncertainty failure; no automatic settlement |
| gmail_tools.py:235 | `_write` / `GmailError('Gmail authorization changed; prior action belongs to a different grant')` | Retained unknown/evidence/previous-uncertainty failure; no automatic settlement |
| gmail_tools.py:240 | `_write` / `GmailError('Gmail request_id was already used for different content')` | Retained unknown/evidence/previous-uncertainty failure; no automatic settlement |
| gmail_tools.py:242 | `_write` / `GmailError('Prior Gmail outcome is uncertain; inspect Gmail before any retry, including a new request_id')` | Retained unknown/evidence/previous-uncertainty failure; no automatic settlement |
| gmail_tools.py:249 | `_write` / `GmailError(f'Gmail {permission} permission was disabled by the owner')` | Retained unknown/evidence/previous-uncertainty failure; no automatic settlement |
| gmail_tools.py:253 | `_write` / `GmailError('An earlier Gmail outcome is uncertain; reconcile it before another mutation')` | Retained unknown/evidence/previous-uncertainty failure; no automatic settlement |
| gmail_tools.py:255 | `_write` / `GmailError('Prior Gmail outcome is uncertain; no duplicate request was dispatched')` | Retained unknown/evidence/previous-uncertainty failure; no automatic settlement |
| gmail_tools.py:262 | `_write` / `GmailError('Google mutation response lacks a valid ID')` | Retained unknown/evidence/previous-uncertainty failure; no automatic settlement |
| gmail_tools.py:267 | `_write` / `GmailError('Gmail outcome uncertain; inspect the mailbox before retrying; no automatic retry was made')` | Retained unknown/evidence/previous-uncertainty failure; no automatic settlement |

GmailAPI transport, OAuth/token refresh, claim failures, prior uncertain actions,
post-POST response validation, and reconciliation evidence failures stay unknown.
No generic GmailError catch is added. Prior uncertain actions still block new
mutations; this patch does not authorize retrying with a new request ID.

## Validation

Before implementation, the new unit rejection matrix produced 18 errors on the
baseline while the authority and post-commit uncertainty controls passed. Supplied
Claude reproduction is preserved as a separate test-only commit. Focused unit and
actual pinned-runtime protocol results are recorded in Memory update.md. Backend
release and runner code are unchanged. No production deployment or historical hold
reconciliation is part of this change.
