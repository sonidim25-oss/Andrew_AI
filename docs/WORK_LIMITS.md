# Owner-requested work limits

Policy requested on 2026-10-17; implemented in `22c80e0` and installed from
`b792b7b` at 00:13:06 UTC on 2026-10-17, together with P1 `18c05f7` and `d1660fa`.

| Operation | Source policy |
| --- | --- |
| Complete autonomous turn, including provider rounds and tools | No total time limit |
| Autonomous session | No duration limit |
| Autonomous cycles per day | No count ceiling; cycles are still recorded |
| Python, Node and shell execution | No host, container or inner execution deadline |
| Waiting for a code/shell runner response | No execution timeout; owner cancellation interrupts the wait |
| Model-provider socket wait | 120 seconds |
| Ordinary owner-message turn | Existing 120-second turn policy |

The owner request supersedes the older 120-second autonomous/worker, 180-minute
session and 150-cycle policies in historical implementation documents. Budget
admission, spending reservations, output allowance (6,000), sandbox CPU/memory/PID
limits and finite browser, connection-setup and administrative waits remain.

## Cancellation and records

The real Letta turn cancellation event is propagated through synchronous tool
callbacks to the runner client. Cancellation closes the client connection; the
runner watches that connection, stops its process group and removes its named
container. Executor ownership is retained until cleanup finishes. An already
cancelled job is rejected before spawning a child. Output capture remains bounded
when the execution deadline is absent.

Session errors and interrupted inputs retain the existing held-state recovery.
The change does not authorize wake, uncertain-message replay, automatic retries,
budget reset, conversation replacement or deletion of memory/spending records.

## Verification and installation boundary

Core suite: 317 tests, 315 passed, 2 optional skipped. New tests cover absent
whole-turn/socket/runner/inner deadlines, continuing beyond the previous session
and cycle caps, provider wait120, bounded output, explicit cancellation and
ownership until cleanup. Independent read-only review found no important defects.

The initial source-only deployment-manifest mismatches were resolved during
owner-authorized installation preparation. All 69 script tests passed, including
37 installer tests and four image/config tests. Independent deployment review
found no important defects.

The installed image is rebuilt with the new `runtime/worker_entry.py`, all three
runtime-service dependencies match source, and all 18 controller modules including
P1 `workers.py` match their tested manifest hashes. Server acceptance passed and
the runtime proof validates the new image/config/package bindings. Installation
backup and evidence: `/var/backups/andrew-incident-fiu2jy4q`.

All 30 pre-existing database tables were compared with the stopped-controller
snapshot; every existing row is unchanged. The only new table is empty
`diary_delivery_claims`. Four messages remain held, and spending is unchanged at
84 usage rows totaling USD 0.12036761. Lifecycle/conversation IDs are unchanged;
installation did not wake Andrew or replay anything.

The runtime acceptance probe now expects a controlled 130-second code job to
finish successfully across the former 120-second boundary. The acceptance client
retains a separate finite 140-second test deadline; it is not production policy.

A separate temporary source fixture ran real Python work for 130 seconds through
both host `run_bounded(timeout=None)` and the inner worker. It completed in
130.105 seconds with no timeout. This verifies those source paths; it is not a
claim about the currently installed container image or production deployment.
