# Independent daily lifecycle

## Release scope

The owner's 2026-10-14 request authorized Andrew's conversation and memory lifecycle without waiting for every future project phase. Subsequent owner instructions authorized the [browser/code runtime](BROWSER_RUNTIME.md) and separately controlled [Gmail integration](GMAIL.md). See [feature status](FEATURE_STATUS.md) for installed versions and evidence.

`TEXT_MEMORY_LIFECYCLE_READY` is the source release gate. `ANDREW_AUTONOMOUS_ENABLED=1` is still required; the fallback additionally requires `ANDREW_FALLBACK_WAKE_ENABLED=1`. Both settings default off. Enabling `ANDREW_ISOLATED_RUNTIME_ENABLED=1` requires a current administrator-owned code/image/policy acceptance record. Gmail uses its separate owner permissions and OAuth scopes.

## Preserved limits and owner controls

- At most 150 started autonomous cycles per Toronto calendar day and 180 minutes per session.
- At most $10 per calendar month, with at least $1 protected reserve. The daily allowance is at most $0.30, accumulating within the month. Lower configured limits are shared by the controller and gateway.
- Each provider call still needs gateway authentication, the allowed model, bounded input/output and a durable budget reservation. Failed or uncertain requests keep their reservations.
- `/pause` blocks model requests and agent mutations once consumed from Telegram. A separate control worker processes owner commands during model work and cancels the active turn; ordinary messages get priority between bounded turns. Only the owner's `/wake` clears pause. Manual wake cancels the previous autonomous session and, when autonomy is enabled, requests a fresh independent session. The scheduler applies the existing budget and daily cycle limits before starting it. When autonomy is disabled, manual wake permits interactive conversation only. Late model results and agent lifecycle actions cannot undo committed owner sleep or pause.
- After an owner reply, Andrew waits five quiet minutes for a follow-up, without model calls. Each further reply extends this persisted deadline. Once quiet, he resumes independent activity within the same limits. Pending owner input anywhere in the durable inbox, and saved messages awaiting delivery, take priority before another independent turn. `/status` distinguishes waiting, independent work, sleep, pause and errors.
- Sleep is an explicit `sleep` or `wake_at` tool choice, blocked while the conversation is active or owner messages await delivery. The sleep state and a brief Telegram notice with the expected wake time are saved in one transaction. Session time/cycle limits stop independent work without changing the sleep flag. Owner `/sleep`, `/wake_at` and `/pause` remain authoritative.
- Timed sleep persists in UTC. Daily fallback at 09:00 America/Toronto starts idle work or wakes indefinite sleep; it respects explicit timed sleep, pause and an active session. Each day's checkpoint runs once, including repeated DST hours and clock rollback.
- During an autonomous session Andrew must save today's diary before voluntarily sleeping. There is one immutable diary per local date, with content checks and validated event references. A confirmed Telegram send is not repeated. An uncertain send requires explicit `/diary retry YYYY-MM-DD` and may duplicate the earlier message.
- Failed turns enter an error state and report “My session failed …; your messages are saved.” Automatic model work and fallback wakes stop; failure never sets the sleep flag. `/wake` resumes work while uncertain messages remain held. Only explicit `/replay` permits uncertain input delivery again, with a duplicate-action warning. A fresh owner message can be answered without replaying earlier uncertain input; a successful reply resumes the quiet-window lifecycle. Owner-requested sleep can also schedule a fresh recovery wake.
- Restart marks unfinished cycles interrupted and retains spending reservations. An interrupted turn enters the same error state, preserving any explicit sleep/pause and avoiding silent retries. An idle session with no unfinished cycle resumes with its original session deadline and conversation quiet window. Interrupted owner-message claims remain held as uncertain. A safe, labeled incident summary can be recorded without another model call.

These conversation/error transitions are implemented in source as of 2026-10-16. They require installation; local verification does not establish the live service version.

Verification for this change: 237 core tests run (235 passed, two optional skips), and all 46 script tests passed with the pinned Letta binary, including the real conversation/quiet-window/sleep callback sequence, cancellation and timeout recovery. All providers used for those protocol tests were isolated local fixtures; no production wake or message replay was requested.

## Local verification

```sh
cd /home/daniel/agent
PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -q
python3 -m unittest discover -s scripts/tests -q
bash -n scripts/enable-daily-lifecycle.sh scripts/validate-and-start-controller.sh
```

`test_lifecycle.py` and `test_conversation_lifecycle.py` cover the release/owner gates, budgets, scheduled and daily wakes, DST and clock rollback, diary delivery, explicit sleep notices, cycle/time ceilings, quiet-window continuation, inbox priority, concurrent owner commands, provider/ledger failure and restart recovery. Local provider/Telegram boundaries are substitutes; they do not prove live integration.

The optional real-harness test starts Letta Code 0.34.13 in a separate temporary backend with an unbilled deterministic provider. It exercises an owner reply through the controller, waiting without another model call, autonomous resumption after the quiet window, diary and explicit sleep callbacks with a saved wake notice, an actual Letta/SQLite restart, preserved IDs/diary/deadline/settlements, a scheduled wake and the `wake_at` callback. It cannot access Andrew's service credentials or database. Run it with the pinned package installed, including its Node dependencies:

```sh
ANDREW_LETTA_TEST_BINARY=/tmp/andrew-letta-protocol-runtime/node_modules/@letta-ai/letta-code/letta.js \
  python3 -m unittest discover -s scripts/tests -p test_letta_protocol.py -v
```

## VPS commissioning

Run the following from the owner's SSH terminal. The Codex shell cannot administer services because sudo requires an interactive password.

```sh
sudo bash /home/daniel/agent/scripts/enable-daily-lifecycle.sh
```

The script installs and compares controller modules, validates startup, and runs two bounded commissioning sessions with the persistent Andrew agent and the real Qwen gateway. Commissioning explicitly uses immediate continuation after a letter reply; the ordinary service retains its five-minute quiet window. The first exercises fallback at the current local minute, saves a diary, delivers it to the configured owner and requests one minute of sleep. Production fallback remains 09:00 Toronto. Letta is restarted between the two separate commissioning processes. The second verifies preserved sleep, diary, conversation and settled ledger, fires the scheduled wake once, requires a new settled charge, and asks Andrew to schedule his next 09:00 wake. Each process allows at most three turns, retains the normal cycle/time/budget gates, and has an eight-minute service timeout.

Only after both stages pass does the script install the owner opt-in environment file and start the ordinary controller with autonomous and fallback schedules enabled. Environment files are used so settings in the original host configuration cannot silently override the commissioning flags. A failure or interruption stops both probes and the controller, writes disabled flags, and retains memory, diary evidence and uncertain spending. No unrelated service is changed.

Evidence is stored privately in `/var/lib/andrew/control/lifecycle-validation.json`; terminal output contains check results and cost totals, without credentials or diary bodies. Existing evidence/configuration makes the script refuse a repeated commissioning run. Inspect that evidence before retrying; do not delete it blindly.

### Retrying the initial failed stage

The initial 2026-10-14 commissioning run installed `7595b7d` and passed startup, but its first live stage stopped with `Agent did not choose sleep`. Both the runtime launch list and input client-tool allowlist were empty. The pinned server intersects these lists and applies them to external controller tools, hiding the diary and sleep tools. The corrected runtime and input both explicitly allow only the nine approved lifecycle and memory tools, while built-in execution and external workers remain excluded.

The first retry installed `a37e2fb` and reached real tool requests, but strict/headless approval rejected `check_budget` and `recent_activity`. The runtime now grants exact permission rules for the same nine controller tools. Strict mode remains enabled, with no wildcard grants; controller argument, diary, pause and budget checks still apply. Retry also restarts Letta before installation to clear failed process state. Approval recovery retains the pinned server's default of not resuming an interrupted turn; stale tool requests are not silently executed. Safe failure check names are mirrored to the journal without remote exception details.

The `7b72dad` attempt then reached approved diary requests, but the client treated the intermediate model-step `stop_reason` as the end of the complete tool turn. Closing that connection interrupted the callback. The client now waits for the actual `turn_finished` event or a committed-sleep abort response, and only returns assistant messages observed in the current turn. Regression tests and the isolated real-harness test reproduced this failure before the change and passed afterward. This protocol proof uses the real pinned Letta engine and synthetic provider responses; a successful paid Qwen/Telegram commissioning run is still required before enabling the daily lifecycle.

After installing this correction, the owner can retry with:

```sh
sudo bash /home/daniel/agent/scripts/enable-daily-lifecycle.sh --retry
```

Retry requires the exact disabled configuration written by the previous failure handler, no completed-stage evidence, and stopped controller/probe units. It recovers the old session and preserves every cycle, charge and uncertain reservation. If today's fallback has already been consumed, it waits for a new one-minute scheduled wake instead of resetting the daily checkpoint. Existing owner pause or timed sleep is not overridden. A failure after a completed stage requires inspecting the saved evidence rather than this initial-stage retry.

## Operational status

Use owner Telegram commands `/status`, `/budget`, `/diary` and `/activity`. Use `/pause` to suspend activity and `/wake` to clear pause. The controller's systemd service must remain running for scheduled wakes; no separate timer is needed.

Live enablement **passed on 2026-10-14 at 23:13 UTC**, using runtime `2971be0`. Both commissioning stages passed with real Qwen and confirmed Telegram diary delivery. The controller is polling with autonomous/fallback settings enabled and isolated runtime disabled. The successful run settled three charges totaling $0.001025076 with no unsettled reservations. The saved next wake is **2026-10-15 09:00 America/Toronto (13:00 UTC)**; that future event has not yet occurred.

Commissioning is complete; do not rerun the commissioning/retry command for routine operation. See [FEATURE_STATUS.md](FEATURE_STATUS.md) for the verified deployment state and exact scope of the live checks. Historical phase specifications describe their original activation restrictions; this document records the newer owner-authorized release scope.
