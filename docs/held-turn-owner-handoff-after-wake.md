# Owner messages after /wake behind a held turn (P0-2 follow-up)

## Incident — 2026-10-22 01:22 UTC

First owner message after the P0-2 rollout and `/wake` failed with
`turn_failed`, and the message was wrongly held as uncertain. Read-only inspection:

- Autonomous scope `3fdb7232…` (session `dca8d74d…`, cycle `39c037e6…`, owner
  revision 40) held `tool_uncertain` since 2026-10-21 12:43. Backend checkpoint
  revision 770: 11 provider steps and 12 tools confirmed; last tool
  `e4759df0…` (`python.run`) still `admitted`.
- On the controller every one of the 24 effects is `confirmed` with a result;
  the last `python.run` has an imported runner receipt. Nothing was actually
  unknown any more; only the backend had not been told.
- Owner revision is 42 (/pause, /wake since). No running cycle.

## Root cause (three defects, all predating P0-2)

1. Importing a saved runner result into the backend (`_runner_tool_receipt`)
   required full resume authority (`_guard`), which refuses as soon as owner
   input waits. The refusal (`ValueError`) ended the whole `reconcile_one` pass,
   so the owner-handoff branch after it was never reached. Stuck since the
   owner message at 2026-10-21 12:45.
2. Owner handoff (`_guard_owner_yield`) only accepted the original owner
   revision or a paused state with `local_recovery_interrupted`. After `/wake`
   neither holds, so no owner action could release the turn.
3. An owner message arriving while a turn was held reached `run_turn`, which
   refuses (`owner_blocked`) whenever recovery work is pending. The controller
   reported that as `turn_failed` and held the message as uncertain even though
   nothing was sent.

## Change

- Backend command `recovery_import_runner_tool` (capability
  `runner_tool_evidence_v1`): evidence-checked claim, then the existing
  `reconcileTool` validation of the controller's runner receipt, then
  transcript projection. Same shape as `recovery_reconcile_provider`: records
  historical evidence only, grants no continuation, accepts no caller result.
  The controller uses it for a held runner tool when available; continuing
  afterwards still needs the ordinary guards.
- `awake_owner_yield`: a newer owner revision may cancel held autonomous work
  for waiting owner input while awake. It requires: not paused or sleeping;
  `autonomy_error` empty or `local_recovery_interrupted`; no active session,
  cycle, owner turn or autonomous turn; no restore fence; matching agent,
  conversation and configuration. It never dispatches. A pending start
  request may exist (it waits behind the held work). Unlike the paused rule
  it ignores `model_turn_progress`, a display hint that outlives gateway calls.
- The controller keeps an owner message queued while journaled work is held
  and replies that it is closing the earlier step first. The dispatch loop
  then imports and hands off; the queued message is answered afterwards.

Unchanged: unknown effects (`admitted` on the controller) still cannot be
cancelled; old work never continues after a newer owner revision; held
messages from earlier failures still need explicit `/replay`.

## Tests

- Real patched backend (`test_runner_backend_protocol.py`): held runner turn
  with a saved result → `/wake` state (revision 1, session cleared, stale
  progress) → import succeeds, no continuation without owner input → owner
  message → handoff with nothing cancelled; 1 provider call, 1 worker call.
  Fails on the production controller and backend at the import step.
- Unit (`test_owner_handoff.py`): awake cancellation fenced against dispatch and
  old authority; 11 refusal states; owner message queued instead of failing.
- Node: wire validation of the new command (87 backend tests pass).

## Recovering the current production state

After rollout: `/wake` (clears `turn_failed`, revision 43), then send any
message. The import and handoff run on the next worker passes; queued messages
(including the 2026-10-21 12:45 one) are then answered. The message from
2026-10-22 01:22 was held as uncertain and needs `/replay` if wanted.
