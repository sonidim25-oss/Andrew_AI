# Reliability review — 2026-10-15

Scope: startup, persistent conversations, daily lifecycle, owner messages, spending,
restart behavior, bounded transport, and Gmail permissions. The production controller
was not stopped or modified during this review. No paid model requests or real email
operations were made by the tests.

## Confirmed issues corrected in source

| Priority | Failure | Correction and evidence |
|---|---|---|
| High | A growing conversation hits the 64,000-byte gateway limit; the generic error ends the Letta turn instead of triggering compaction. | Return the context-overflow category understood by the pinned SDK. An isolated real-SDK 20-turn stress test failed before the change and passes afterward, including actual overflow and recovery. Every admitted provider request remains within the same limit. |
| High | Telegram polling failure skips saved inbox work and autonomous session ticks. | Independently guard polling, saved updates, wakes, queued messages, sessions and reply delivery. Fault-injection tests verify that a failed poll does not stop these stages. |
| High | A failed Telegram reply leaves the input pending, allowing the same model/tool action to run again. | Persist a processing claim before acting; atomically save the response and complete the input before delivery. Retry the saved response, not the action. Interrupted claims retain the original input in `telegram_uncertain` and produce a notice. Tests cover send failure, database reopen and interruption after a claim. |
| High | A large sleep-message batch is truncated, but every message in the batch is marked released. | Claim complete messages in bounded batches; leave the remainder queued. A multi-batch Unicode test verifies every message reaches the model once. |
| Medium | The commissioning fixture inherits Gmail enablement from the production policy, while its fake provider rejects the extra tools. | Explicit fixture policy, plus separate real-SDK browser/budget/Gmail callback coverage. The previously failing browser check now passes while production Gmail remains enabled. No production policy was changed. |
| Medium | Malformed billing values can throw during settlement, poison totals, or produce invalid JSON for Letta. | Validate finite nonnegative charges and SQLite-sized token counts. Charge the existing full reservation for malformed usage and return valid JSON usage. Invalid provider envelopes retain their reservation and return a safe error. |
| Medium | Busy owner chat can monopolize the loop; the rate limiter discards inputs. | Defer saved inputs and process one ordinary owner request per iteration. Pause/wake/status/budget remain eligible within the fetched batch. Regression tests cover deferral and scheduler progress. |
| Medium | Socket inactivity timeouts do not enforce a total read deadline; fragmented WebSocket messages evade the per-frame size cap. | Enforce the deadline during partial reads and bound the assembled message. Close failed handshakes. Tests exercise both failure paths. |
| Medium | Shutdown can begin another autonomous turn after SIGTERM arrives during polling. | Check the stop flag between stages and requests. Allow 300 seconds for in-flight shutdown in the service unit. The stop-during-poll test verifies no new model work starts. |
| Low | Emoji-heavy Telegram messages can exceed Telegram's UTF-16 length limit despite Python character truncation. | Bound outgoing text by UTF-16 units; regression covers astral emoji. Existing truncation behavior remains. |

Three pre-existing unit-test failures were stale fixtures: two startup doubles lacked
the capability-instruction update command, and one memory-context assertion assumed
there was no budget-context message. These fixtures now check the current behavior.

## Verification

- Prototype suite: **186 tests run, 184 passed, 2 explicitly skipped**. The skips
  require live Letta/Qwen integration settings; they are not counted as passes.
- Administrator and isolated protocol suite: **25 tests passed**, using the pinned
  Letta 0.34.13 package with temporary backends and deterministic, unbilled providers.
- Coverage includes existing sleep/wake, owner pause, cycle/session ceilings, budget
  rejection, reservation persistence, memory backup encryption/restore and diary
  delivery/restart tests.
- Twelve new offline Gmail tests cover separate permissions, OAuth scopes, refreshed
  tokens, disconnect-safe caching, mutation deduplication across reopen, uncertain
  results, revocation before a mutation, read-only requests, Trash-only deletion and
  mail-header injection. A real-SDK test also exercises the Gmail status callback
  with a fixture implementation.
- `git diff --check` passed.

Commands, from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=prototype \
  ANDREW_LETTA_TEST_BINARY=/tmp/andrew-letta-protocol-runtime/node_modules/@letta-ai/letta-code/letta.js \
  python3 -m unittest discover -s scripts/tests -q
```

## Production observations and remaining limits

At approximately 02:30 UTC, controller, Letta, runner, browser and egress services
were all active with **zero automatic restarts**. Controller memory was about 20 MiB
of its 512 MiB limit, and Letta about 194 MiB of its 2 GiB limit. The host had about
14 GiB available RAM and 59 GiB free on `/`; the dedicated projects volume was
essentially empty. This is a point-in-time observation, not an uptime guarantee.

The owner subsequently completed commissioning at **02:38 UTC**. Installed modules
matched **95e4ae4 plus the reviewed uncommitted changes**. Network/resource checks,
public HTTPS, browser/account fixtures, both real isolated Letta callback tests and
startup validation passed. IDs, owner pause/sleep, cycles, memory and spending rows
were preserved. Evidence: `/var/lib/andrew/runtime-commissioning/20261015T023521Z`.
The manual-wake correction and reliability changes are **now installed**. The changes
were uncommitted at installation and are captured in the commit containing this report. A subsequent read-only service check confirmed all five
services active with zero automatic restarts and `TimeoutStopUSec=5min` loaded.
This supersedes the failed 02:11 attempt recorded at
`/var/lib/andrew/runtime-commissioning/20261015T021150Z`.

Outstanding operational limits:

- Actual Qwen, Telegram and Google behavior after installing this patch still needs
  observation. Offline and isolated tests do not prove external-service availability.
- Real Gmail message reads, refreshes and mutations were not exercised in this review.
- A failed or interrupted autonomous model turn still rests instead of blindly
  repeating potentially completed actions. Recovery is through the existing fallback
  schedule or owner `/wake`; uncertain spending remains reserved.
- Tool calls are synchronous. In particular, the runner client and worker both use
  a 120-second limit, while worker cleanup can take additional time. Work close to
  that ceiling can yield a transport timeout before the cleanup response. Container
  execution limits remain enforced; this review does not claim uninterrupted work
  through such a timeout.
- Extremely large chat backlogs can delay a control command beyond the first 50
  persisted inputs. Remote calls already in progress cannot be instantly interrupted
  by a later Telegram pause command.
- A Telegram response may be delivered twice if the first acknowledgement is lost.
  Retries are labeled; the model/tool action is not re-executed.

## Installation command (completed by the owner)

The owner ran this command from the SSH terminal; it does not need repeating for this installation:

```sh
sudo bash /home/daniel/andrew-browser-integration/scripts/enable-browser-runtime.sh
```

The established installer performs kernel/browser acceptance, both isolated browser
callback scenarios, installed-module comparison and controller startup validation.
It binds the new package to the runtime proof and preserves owner pause/sleep,
memory and spending state. A reboot alone does not install these changes. The
interactive administrator password was supplied in the owner’s terminal; Codex verified service state afterward.
