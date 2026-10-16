# Code review — 2026-10-16

Reviewed source: `3acf8c3` on `main`, including the recent P1 and P2 fixes.
Scope: owner controls, durable input/output, lifecycle, Letta turn handling,
budget admission, memory, and related transports. This is a targeted review,
not a claim that every subsystem is defect-free.

All four findings below were reproduced with temporary SQLite databases and
fake model/Telegram dependencies. No live model requests, messages, service
changes, or production data changes were made. Application source is unchanged.

## R1 — P1: Saved pause commands stop working during Telegram polling failures

Location: `prototype/andrewbot/dispatch.py:66–73`, with the main-thread skip at
`dispatch.py:95–96`.

The control poller processes the durable control inbox only after `get_updates`
succeeds. When that network request raises, it skips saved controls. Meanwhile,
the main thread deliberately skips all such controls whenever the poller exists.
A pause already saved to SQLite can consequently remain pending throughout a
Telegram outage while the autonomous scheduler continues running.

Reproduction: enqueue an authenticated `/pause`, start the dispatch threads with
`get_updates` raising `RuntimeError`, and invoke ordinary `process_updates`.
Observed: `paused=0`, one pending input. The saved pause was never executed.

Correction direction: drain saved controls independently of polling success.
Add a threaded regression with a persisted pause and failed network polls.

## R2 — P2: Control prioritization reverses lifecycle command order

Location: `prototype/andrewbot/dispatch.py:69–73` and `dispatch.py:95–96`.

Newly received controls execute before older saved controls. Separately, the
background thread handles `/wake` while `/sleep` remains for the main thread.
Both paths can apply an older lifecycle instruction after a newer one.

Reproductions:

- One batch containing input 1 `/sleep`, then input 2 `/wake`: execute the
  poller's control pass followed by the main inbox pass. Observed:
  `sleeping=1`, `session_start_requested=''`, although the last command was wake.
- Persist input 1 `/pause`; then receive input 2 `/wake`. Execute newly received
  controls followed by the saved-control pass, as `_poll_loop` does. Observed:
  `paused=1`, `session_start_requested=''`, although the last command was wake.

Correction direction: preserve lifecycle ordering across new and saved inputs
and across both processing threads, while retaining prompt pause handling when
ordinary model work is blocked. Merely swapping the two poller passes does not
fix the `/sleep`/`/wake` case.

## R3 — P2: A valid diary can lose its ending and still be marked delivered

Location: `prototype/andrewbot/telegram.py:32–36`; confirmation occurs at
`prototype/andrewbot/diary.py:123–129`.

Diary validation permits 3,000 Unicode characters. Telegram transport silently
cuts outgoing messages to 4,000 UTF-16 units. Valid content with enough astral
characters exceeds this transport limit; no continuation or truncation notice
is sent. Diary delivery then marks the whole entry delivered, suppressing any
automatic delivery of the omitted text. Long ordinary replies also lose text.

Reproduction: save a 2,152-character diary containing 2,100 emoji and a final
sentence, then deliver it through the real `send_message` with a fake API call.
Observed: only 2,024 characters sent, final sentence absent, SQLite delivery
state `delivered`. The complete original remains stored in SQLite.

Correction direction: deliver all text in bounded chunks and define durable
partial-delivery/retry behavior; only confirm complete diary delivery once all
parts have been acknowledged.

## R4 — P3: Bot-suffixed history commands search for the bot name

Location: `prototype/andrewbot/controller.py:448`.

`handle_update` accepts slash-command bot suffixes, but history search extracts
its query by removing a fixed eight characters from the original command.
`/history@AndrewBot example.com` therefore searches for
`@AndrewBot example.com`; `/history@AndrewBot` searches for `@AndrewBot` rather
than returning recent history.

Reproduction: pass the authenticated suffixed command to `handle_update` with
a mocked visit-history backend. Observed search argument:
`'@AndrewBot example.com'`.

Correction direction: extract the query after the full first command token.
Cover ordinary, suffixed, uppercase, and no-query command forms.

## Validation limits

The existing core suite still passes: 210 tests run, 208 passed, 2 skipped.
Its current assertions do not cover these four reproductions. The reproductions
were standalone offline probes; no failing tests were added to the normal suite.
No fixes, commits, pushes, or installations were performed during this review.

## R1 follow-up — source correction, 2026-10-16

The control worker now drains saved controls before each network poll, using a
separate guarded operation. Failed polls cannot bypass the saved-input pass.
Shutdown is checked again before starting the subsequent network request.

The new threaded regression persists a pause behind 100 ordinary messages while
an autonomous model turn is blocked and Telegram polls repeatedly fail. It checks
that pause cancels the turn, sends its acknowledgement through a working fake
send endpoint, completes the saved control, records the cycle as stopped, and
prevents another autonomous call. The test failed on the reviewed code and passes
with this change. Telegram reply delivery still depends on the send endpoint
being available; applying the saved pause does not.

Full verification: 211 core tests run, 209 passed, 2 skipped; 38 script tests
passed with the pinned isolated Letta fixture enabled. The other findings remain
open. This source correction has not been installed in the live service.

## R2 and R3 follow-up — source corrections, 2026-10-16

R2: all authenticated local owner commands now use the same control worker and
durable input order. Newly received commands no longer bypass older saved ones.
Wake-time prompts are re-evaluated after each input so a plain time response is
processed before a later wake command. Routing and input claims share a short
lock, released before model work. A main-thread inbox snapshot cannot reclaim
an input already completed by the control worker. Ordinary chat claimed before
a new time prompt cannot later become an out-of-order scheduling response.

R3: outgoing text is split by UTF-16 length without dropping characters. A new
additive SQLite table keeps the original rendered message, the next part to
send, and the current part's attempt count. Acknowledgements are persisted
between parts, so restart/retry skips confirmed parts and labels an uncertain
part when repeating it. A partial diary retains its uncertain state and still
requires the owner's existing explicit retry command. Only complete delivery
clears the durable progress and confirms the parent outbox/diary record.

Regressions cover both ordering reproductions, typed and button commands,
interactive/invalid wake-time responses, stale main-thread inbox snapshots,
sleep/wake during blocked model work, a complete emoji-heavy diary, partial
diary and reply failure across database reopen, and restart after the last
acknowledgement but before final diary confirmation. Full verification:
218 core tests run, 216 passed, 2 skipped; 38 script tests passed with the pinned
isolated Letta fixture enabled. R4 remains open. These fixes have not been
installed in the live service.

## R4 follow-up — source correction, 2026-10-16

History search now takes its query after the complete first command token,
including any bot suffix, instead of removing a fixed number of characters.
The regression failed before the fix and covers ordinary and bot-suffixed
commands, uppercase commands, whitespace, multiword queries, no query, and the
history button. History commands make no model calls. Full core verification:
219 tests run, 217 passed, 2 skipped. This correction is not installed in the
live service. All four review findings now have source corrections recorded
here; live installation and observation remain separate work.
