# Journaled retry after a provably unsent provider request (P0-2, first narrowing)

## Problem

In journaled recovery mode (production, `ANDREW_LOCAL_RECOVERY_ENABLED=1`) every
provider failure except an explicit HTTP 429 became `provider_uncertain`. The
backend held the turn, the session stopped with `local_recovery_interrupted`,
and Andrew waited for the owner. This included failures that the transport had
already proved happened before any request byte left (DNS failure, refused
connection, unreachable proxy, connect timeout; see `TransportEvidence` in
`model.py`, reviewed in `534d6f4`). Legacy mode already refunded those (503
`provider_not_reached`); journaled mode deliberately did not, because the backend
only accepted HTTP 429 as a retryable rejection (big fixes P0-1 remainder).

## Change

A proven-unsent failure is now a second kind of retryable provider rejection,
using the existing 429 backoff path unchanged:

- The gateway, in journaled mode only, calls
  `RecoveryRecords.reject_provider(admission_id, reason='provider_not_reached')`
  when `ProviderRequestError.request_sent` is false. One SQLite transaction
  records the receipt, deletes the step's reservation (nothing was sent, so
  nothing was spent) and marks the effect `rejected`. It returns 503
  `provider_not_reached` with `retry_at_ms`.
- `recovery_provider_rejections` gains `reason` (`rate_limited` | `provider_not_reached`).
  A CHECK enforces `rate_limited` ⇔ `http_status IS 429` and
  `provider_not_reached` ⇔ `http_status IS NULL`. Existing databases are
  rebuilt once on Store open; every existing row becomes `rate_limited`
  with all values unchanged.
- Proofs for HTTP 429 keep their exact previous shape (no `reason` key), so
  checkpoints already stored by the backend still compare equal.
- The backend's `validRejection` accepts a proof with
  `reason: 'provider_not_reached'` and `http_status: null`. A proof without
  `reason` must still be HTTP 429. Any other combination still holds.
- Backoff is the existing schedule, shared with 429: 5 s doubling to a 320 s cap.
  Each retry uses one of the backend's 1,024 work steps for the turn, so a
  continuous outage of roughly 90 hours ends in the existing `storage_limit` hold.
- `/status` reports "Provider not reached (request was not sent); retry eligible
  at …" and "awake, waiting to reconnect to the provider".

## Unchanged (still held for the owner)

- Any failure where request bytes may have been sent: timeouts after send,
  connection resets, 5xx and other HTTP errors, invalid responses, redirects or
  proxy tunnels followed by failure.
- A retry whose own outcome is lost: the effect is `admitted` again before
  network I/O, so an old rejection never authorizes a replay.
- All tool uncertainty (runner, Gmail, media, diary).
- Retries still need current owner authority: pause, sleep, owner revision
  change, pending owner input, restore fences and lease expiry all block dispatch.
- Legacy (non-journaled) mode.

## Deployment

The controller (`gateway.py`, `recovery_records.py`, `controller.py`) and the
backend (`letta_recovery/contracts.mjs`) must ship together. The mixed
combination was tested: a new controller with the currently installed backend
release `94ccc2cc…` holds the turn with zero reservations and no resend, the
same outcome as before this change. An old controller never writes the new receipt.

## Tests

- Node: 2 new (`provider-backoff.test.mjs`); both fail on the old contract.
  All 86 backend tests pass.
- Controller: 7 new in `test_provider_backoff.py` (typed receipt plus restart,
  repeated backoff then lost retry held, owner authority, constraint and
  argument validation, migration of an old-schema 429 database, `/status`,
  restore). 2 in `test_provider_transport.py` use real sockets: a refused port
  backs off and later dispatches exactly once; a send-then-reset stays 422 and
  held with one POST. The previous test asserting the journaled hold was
  replaced. All new tests except the maybe-sent guard fail on the old code.
- Real patched backend (`test_local_recovery_crash_protocol.py`): the first
  provider call goes through the real `OpenRouterModel` transport to a closed
  port; the turn backs off, retries and completes with 2 provider calls, 1 tool
  effect, 0 reservations, one `provider_not_reached` receipt and no autonomy error.
