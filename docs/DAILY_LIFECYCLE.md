# Independent daily lifecycle

## Release scope

The owner's 2026-10-14 request authorizes enabling Andrew's existing conversation and memory lifecycle. It supersedes the earlier requirement to finish every future project phase before activating any autonomy. It does not approve autonomous shell, browser, site-account, social or email actions.

`TEXT_MEMORY_LIFECYCLE_READY` is the source release gate. `ANDREW_AUTONOMOUS_ENABLED=1` is still required; the fallback additionally requires `ANDREW_FALLBACK_WAKE_ENABLED=1`. Both settings default off. Startup refuses autonomous operation with `ANDREW_ISOLATED_RUNTIME_ENABLED=1`. This release does not mark other project phases complete.

## Preserved limits and owner controls

- At most 150 started autonomous cycles per Toronto calendar day and 180 minutes per session.
- At most $10 per calendar month, with at least $1 protected reserve. The daily allowance is at most $0.30, accumulating within the month. Lower configured limits are shared by the controller and gateway.
- Each provider call still needs gateway authentication, the allowed model, bounded input/output and a durable budget reservation. Failed or uncertain requests keep their reservations.
- `/pause` blocks model requests and agent mutations once consumed from Telegram; commands are processed between bounded turns. Only the owner's `/wake` clears pause. Manual wake cancels the previous autonomous session and permits interactive conversation; it does not resume that session. Late agent actions cannot cancel committed sleep.
- Timed sleep persists in UTC. Daily fallback at 09:00 America/Toronto starts idle work or wakes indefinite sleep; it respects explicit timed sleep, pause and an active session. Each day's checkpoint runs once, including repeated DST hours and clock rollback.
- During an autonomous session Andrew must save today's diary before voluntarily sleeping. There is one immutable diary per local date, with content checks and validated event references. A confirmed Telegram send is not repeated. An uncertain send requires explicit `/diary retry YYYY-MM-DD` and may duplicate the earlier message.
- Restart marks unfinished cycles interrupted, retains spending reservations and rests an interrupted session. It preserves an existing timed sleep and never silently replays an uncertain model call. A safe, labeled incident summary can be recorded without another model call.

## Local verification

```sh
cd /home/daniel/agent
PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -q
python3 -m unittest discover -s scripts/tests -q
bash -n scripts/enable-daily-lifecycle.sh scripts/validate-and-start-controller.sh
```

`test_lifecycle.py` covers the release/owner gates, external-runtime rejection, budgets, scheduled and daily wakes, DST and clock rollback, diary delivery, sleep persistence, cycle/time ceilings, provider failure and interrupted-session recovery. Local provider/Telegram boundaries are substitutes; they do not prove live integration.

## VPS commissioning

Run the following from the owner's SSH terminal. The Codex shell cannot administer services because sudo requires an interactive password.

```sh
sudo bash /home/daniel/agent/scripts/enable-daily-lifecycle.sh
```

The script installs and compares controller modules, validates startup, and runs two bounded commissioning sessions with the persistent Andrew agent and the real Qwen gateway. The first exercises fallback at the current local minute, saves a diary, delivers it to the configured owner and requests one minute of sleep. Production fallback remains 09:00 Toronto. Letta is restarted between the two separate commissioning processes. The second verifies preserved sleep, diary, conversation and settled ledger, fires the scheduled wake once, requires a new settled charge, and asks Andrew to schedule his next 09:00 wake. Each process allows at most three turns, retains the normal cycle/time/budget gates, and has an eight-minute service timeout.

Only after both stages pass does the script install the owner opt-in environment file and start the ordinary controller with autonomous and fallback schedules enabled. Environment files are used so settings in the original host configuration cannot silently override the commissioning flags. A failure or interruption stops both probes and the controller, writes disabled flags, and retains memory, diary evidence and uncertain spending. No unrelated service is changed.

Evidence is stored privately in `/var/lib/andrew/control/lifecycle-validation.json`; terminal output contains check results and cost totals, without credentials or diary bodies. Existing evidence/configuration makes the script refuse a repeated commissioning run. Inspect that evidence before retrying; do not delete it blindly.

### Retrying the initial failed stage

The initial 2026-10-14 commissioning run installed `7595b7d` and passed startup, but its first live stage stopped with `Agent did not choose sleep`. Both the runtime launch list and input client-tool allowlist were empty. The pinned server intersects these lists and applies them to external controller tools, hiding the diary and sleep tools. The corrected runtime and input both explicitly allow only the nine approved lifecycle and memory tools, while built-in execution and external workers remain excluded.

The first retry installed `a37e2fb` and reached real tool requests, but strict/headless approval rejected `check_budget` and `recent_activity`. The runtime now grants exact permission rules for the same nine controller tools. Strict mode remains enabled, with no wildcard grants; controller argument, diary, pause and budget checks still apply. Retry also restarts Letta before installation to clear failed process state. Approval recovery retains the pinned server's default of not resuming an interrupted turn; stale tool requests are not silently executed. Safe failure check names are mirrored to the journal without remote exception details.

After installing this correction, the owner can retry with:

```sh
sudo bash /home/daniel/agent/scripts/enable-daily-lifecycle.sh --retry
```

Retry requires the exact disabled configuration written by the previous failure handler, no completed-stage evidence, and stopped controller/probe units. It recovers the old session and preserves every cycle, charge and uncertain reservation. If today's fallback has already been consumed, it waits for a new one-minute scheduled wake instead of resetting the daily checkpoint. Existing owner pause or timed sleep is not overridden. A failure after a completed stage requires inspecting the saved evidence rather than this initial-stage retry.

## Operational status

Use owner Telegram commands `/status`, `/budget`, `/diary` and `/activity`. Use `/pause` to suspend activity and `/wake` to clear pause. The controller's systemd service must remain running for scheduled wakes; no separate timer is needed.

Live enablement is **pending** until the administrator command succeeds and its output is reviewed. See [FEATURE_STATUS.md](FEATURE_STATUS.md) for the current verified deployment state. Historical phase specifications describe their original activation restrictions; this document records the newer owner-authorized release scope.
