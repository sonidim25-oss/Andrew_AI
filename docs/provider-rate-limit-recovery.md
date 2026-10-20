# Provider rate-limit recovery

The 2026-10-18 15:18:59 UTC autonomous input passed transcript projection, then
received HTTP 429 at 15:19:11 UTC. The previous gateway treated all provider
exceptions as unknown outcomes and retained the reservation. The recovery
protocol could not distinguish explicit rejection from lost response, so it
stopped indefinitely under `local_recovery_interrupted`.

OpenRouter documents HTTP 429 as rate limiting and supports the standard
`Retry-After` header:
[OpenRouter error documentation](https://github.com/OpenRouterTeam/docs/blob/main/api_reference/errors-and-debugging.mdx).
The original response body and headers were not retained, so the precise upstream
rate-limit source for this incident is unknown.

## Implemented behavior

- For journaled provider and compaction requests, an explicit HTTP 429 records an
  immutable rejection receipt and releases its reservation in one SQLite transaction.
  Error bodies, arbitrary headers, credentials, and reasoning are not retained.
- Each receipt binds the effect admission, rejection attempt number, HTTP status,
  rejection time, and retry time. The effect retains its exact request digest.
- The backend persists `provider_backoff`, retaining the original input, run,
  provider step, and request. It waits without consuming the work-step budget.
- Retries use fresh execution authority. The controller checks scope, request
  digest, owner revision, pause/sleep, pending owner input, restore fences, and
  lease validity before moving the effect back to `admitted` and reserving cost.
- Delay defaults to five seconds, doubles on successive rejections, and caps the
  local exponential delay at 320 seconds. A valid provider `Retry-After` delay is
  never shortened. Seconds and HTTP-date formats are supported. Each provider
  attempt remains bounded by the existing transport timeout.
- A lost response after retry is still unknown. Prior rejection receipts cannot
  authorize replay while the current effect is admitted. Unknown tools, timeouts,
  malformed responses, and other HTTP statuses retain the existing hold behavior.
- `/status` exposes HTTP 429 and retry eligibility time. Diagnostics expose only
  rejection metadata. Backup restoration preserves newer receipts and refuses
  divergent histories.

Restart recovery leaves transient execution-lock conflicts and stale checkpoint
revisions pending for the next scheduled worker pass instead of misclassifying
them as permanent corruption. This does not authorize an unknown provider retry.

Automatic recovery still requires the original owner authority. Pause and a
subsequent wake change owner revision; they do not silently authorize continuing
an old input. This change does not supply a general override for held work.

## Selected legacy incident migration

The deployed old gateway has no rejection receipt. The installer therefore offers
an explicit operator migration, not automatic classification of old activity logs.
The read-only preflight matched admission
`4177fdeadc1d4b47853b89440e6e1688`, cycle
`fcb564aba000404591b69a9e2d01dd83`, event 338, and reservation $0.0138654.

Migration is limited to a conversation without an earlier completed journaled
turn; a completed predecessor requires separate reconciliation. Migration requires
owner pause, one pending autonomous input, one matching provider
admission and reservation, one unique HTTP 429 gateway event since its checkpoint,
no competing later provider admissions or usage, no tool attempts, matching model
input receipt, and a matching backend first-provider hold and original payloads.
It rechecks evidence after both services stop and the paired backup is saved.
Backend and controller writers run as their existing service users.

The backend checkpoint is archived under its original binding; transcript and
projection are unchanged. The controller retains the input and effect records,
marks them retired, records the operator evidence, ends the interrupted cycle,
and removes only that rejected request's reservation. Ten older reservations and
15 held owner messages are preserved. No request is sent by migration.

If interrupted after backend archival, migration refuses automatic rerun. Both
services remain stopped on failure; inspect `paired-before`, the retained archive,
and `*-rate-limit-retirement.txt` before reconciliation. Never delete checkpoints,
reservations, or held messages to force the installer through.

## Deployment

Keep Andrew paused. The combined artifact includes the previous transcript fix.
The saved candidate passed all acceptance stages: 56 backend tests, 546 controller
tests (2 skipped), 173 integration tests, and 33 original-runtime compatibility
tests (16 skipped). Production deployment is still pending. From the server terminal:

```bash
cd /home/daniel/andrew-autonomy-media
sudo python3 scripts/install-letta-recovery.py \
  --backend-release "$PWD/.superpowers/sdd/2026-10-18-local-letta-durable-recovery/release-provider-backoff-verified" \
  --retire-rate-limited-input 4177fdeadc1d4b47853b89440e6e1688 \
  --apply
```

The installer validates a fresh candidate against the then-current deployment,
runs its full acceptance suite, and prints a new `/var/backups/andrew-local-recovery-...`
directory. Only after `installed: true`, enable with that exact printed directory:

```bash
sudo python3 scripts/install-letta-recovery.py --enable /var/backups/andrew-local-recovery-REPLACE_WITH_PRINTED_ID
```

Enable preserves pause and does not request a wake. After `enabled: true`, use
the owner's `/wake` once. A status showing a future rate-limit retry time means
backoff is active; it is not a reason to restart or repeatedly wake Andrew.

Verify the live result with:

```bash
sudo python3 scripts/diagnose-session.py --current --brief --letta-recovery-metadata
```

Success requires a completed bound model-turn receipt and settled provider usage
or a confirmed lifecycle tool action. Service health and passing offline tests
alone do not prove a successful production turn. The fifteen older held messages
are intentionally not a success metric for this change.
