# Owner-requested work limits

Source policy requested on 2026-10-17; implemented in `22c80e0` on
`fix/remove-work-deadlines`. This revision is not installed on production.

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

The 59-method script suite has ten source-hash subtest failures in
`test_checked_in_deployment_manifest_matches_prepared_sources`; the other 58
methods pass. These are controller/appserver/runner and P1 delivery changes that
are newer than the installed deployment manifest. The installer remains fail-closed.

Installing this source requires a worker image rebuilt with the new
`runtime/worker_entry.py`, deployment of `runner_service.py`, `process_limits.py`
and `runner_client.py` in the runtime-service package, and all changed controller
modules including the P1 `workers.py` dependency. Update image/config/manifest
bindings and rollback coverage, then rerun sandbox and isolated protocol acceptance
before renewing the runtime proof. The prior image release record is retained;
its old worker code must not be represented as containing this change.

The runtime acceptance probe now expects a controlled 130-second code job to
finish successfully across the former 120-second boundary. The acceptance client
retains a separate finite 140-second test deadline; it is not production policy.

A separate temporary source fixture ran real Python work for 130 seconds through
both host `run_bounded(timeout=None)` and the inner worker. It completed in
130.105 seconds with no timeout. This verifies those source paths; it is not a
claim about the currently installed container image or production deployment.
