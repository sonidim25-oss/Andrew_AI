# Message latency: proposed implementation design

Status: design approved. P0 timing and independent delivery are installed on production (23:08 and 23:31:49 UTC on 2026-10-16). P1 worker, interactive timeouts and local health are implemented on feature branch fix/message-worker-p1; independent source review is complete and both shutdown findings are fixed. Deployment preparation remains pending. P2 Gmail remains pending.

## Intent and constraints

Implement `plan.md` so local owner controls remain responsive during model work
and slow Telegram delivery, and expose where latency occurs. Preserve existing
working-tree changes, durable messages, memory, conversation IDs and spending
records. Do not automatically replay uncertain inputs or deliveries. Use isolated
fixtures for verification; do not wake Andrew or make paid requests for tests.

## Findings in the existing implementation

- `dispatch.py` already handles local owner controls on a background poll/control
  thread. `process_updates()` releases the routing lock before normal model work.
  Existing tests exercise `/status` while the model is blocked.
- `step()` synchronously handles normal updates, scheduled checks,
  `flush_queued()` and `sessions.tick()`. Long work delays subsequent stages.
- All dispatch replies share one durable FIFO and one delivery lock. A slow
  ordinary reply delays a system reply even when its command has finished.
- `TelegramAPI.call()` uses the same default 35-second socket timeout for sends
  and polls. A socket timeout is not a guaranteed total operation deadline.
- `main.py` delivers diary messages synchronously outside `dispatch.step()`;
  this path also needs coverage when separating slow work.
- `deliver_saved_message()` tracks acknowledgements per part, but retries an
  unacknowledged attempted part automatically with a warning prefix. New delivery
  handling must hold uncertain attempts rather than silently inherit that policy.

These are code-path findings. They do not establish the cause of a new Letta
failure or measure current production latency.

## Recommended sequence

### First increment: P0 timing, without changing scheduling

Add a thread-safe, bounded in-memory timing collector with monotonic timestamps.
Measure polling, inbox waiting since local receipt, control handling, normal
handling, queued-message processing, schedule checks, autonomous work, outbox
waiting, and individual Telegram sends. Distinguish long polling from processing.
Keep fixed stage names, counts, durations, in-flight age and fixed error classes;
do not collect bodies, credentials, tool arguments or raw exception messages.
Distinguish current-process queue age from persisted timestamps across restarts;
never subtract monotonic timestamps from different processes.

Instrument existing boundaries rather than introducing new execution threads in
this increment. Use fake clocks and blocked fixtures to verify attribution,
bounded retention, concurrent snapshots and recording on exceptional exit.

### Second increment: independent durable system delivery

Add a backwards-compatible outbox lane with existing rows defaulting to normal.
Route authenticated local-command replies to the system lane. Give each lane a
single sender and independent ownership; preserve order within each lane and
document that system replies may overtake ordinary replies. Never allow both
senders to select the same row. Keep owner commands themselves ordered.

Before enabling new senders, persist a held state for attempted parts lacking an
acknowledgement. On restart, hold such deliveries rather than replay them.
Acknowledged parts remain acknowledged. Definitively unsent work remains pending.
Test migration, restart between send and acknowledgement, duplicate claims,
multipart messages and a blocked normal sender alongside a system reply.

### Third increment: P1 worker, timeouts and health

Use one serialized model worker shared by normal input, queued input and autonomy;
do not introduce concurrent Letta turns. Keep durable input claims and prioritize
owner input before starting another autonomous turn. Keep the main scheduler and
local controls responsive, bound admitted work, and avoid claiming inputs merely
to place them in a volatile queue. Cancellation, shutdown and restart must retain
the existing uncertain-input hold behavior. Move diary network delivery off the
main scheduler with exclusive delivery ownership.

Separate poll and interactive network settings. Start with a five-second
interactive socket timeout and a polling timeout that exceeds the requested
long-poll duration. Treat an interrupted send as uncertain, never as permission
to retry automatically. Rate-limit responses must not cause retry storms.

Add owner-only `/health` using local snapshots: worker state, queued counts,
held counts, stage durations, oldest pending age and last success. It performs
no model request or synchronous remote health probe and uses the system lane.

### Fourth increment: P2 Gmail

Select explicit read-only Gmail checks after reviewing the existing broker and
permissions. Do not introduce sending, permission changes or broader access as
part of direct commands. Gmail network work must not block the control worker.

## Alternatives and verification

Only adding timing is the smallest safe first increment but does not remove
blocking. A priority FIFO alone cannot bypass an already blocked send. Separate
durable lanes plus a single model worker address both bottlenecks while retaining
model serialization. A general async rewrite would increase migration scope and
is unnecessary for this plan.

Run existing dispatch, delivery, lifecycle, budget and protocol regressions plus
deterministic blocked-worker/blocked-sender tests. Review shutdown ownership before
closing storage. Prepare installation and rollback separately; source validation
does not establish production latency or authorize replay of held messages.
