# Andrew Feature Status

**Verified:** 2026-10-14 UTC. The earlier successful integration snapshot is from 22:29 UTC. Daily-lifecycle commissioning passed installation/startup at 22:50 UTC and on retry at 22:56 UTC, but both attempts failed the first live stage. The controller is stopped with autonomous/fallback settings disabled; the corrected visibility and exact permission rules are locally verified and await retry.

**Installed runtime revision:** `a37e2fb`, confirmed by the administrator's module comparison at 22:56 UTC. Earlier controller/integration fixes were published through GitHub `main` at `84fa2b1`; lifecycle commits and the newest permission correction have not been published. The permission correction is not yet installed.

This is the current feature-status source for Andrew. Source implementation, passing tests, installation, live behavior and autonomous enablement are separate claims. VPS state is a dated observation and must be refreshed after deployment changes.

## Status matrix

| Feature area | Implemented in source | Passed local tests | Installed on VPS | Passed live integration tests | Enabled for autonomous use |
|---|---|---|---|---|---|
| Letta agent and Qwen model gateway | Yes. Persistent agent, provider registration, WebSocket model updates and controller-owned model gateway. | Yes. App Server, model and gateway coverage. | Yes. Letta and controller services are active; deployed controller modules matched the verified checkout. | Yes for the configured Qwen path: agent/runtime startup, a real Qwen response, and recall in the same saved conversation after restarting Letta and the controller. Other model providers were not exercised live. | No. The project-wide autonomous activation gate is closed. |
| Owner Telegram conversation | Yes. Owner checks, polling, commands and model replies. | Yes. Controller and Telegram tests. | Yes. Controller is polling Telegram. | Yes. The owner received the supplied test phrase in Andrew's model response; the owner message and assistant reply were also verified in controller SQLite. | No. Owner-initiated conversation is enabled; unattended sessions remain gated. |
| Budget gateway and spending ledger | Yes. Authentication, model allowlist, token cap, budget reservations and settlement. | Yes. Gateway/budget coverage includes rejection before provider calls and avoiding duplicate accounting. | Yes, in the active controller. | Yes for live accounting and restart persistence. Existing ledger rows were unchanged after restart; a new Qwen recall settled one charge with positive token counts and cost, leaving zero unsettled reservations. Budget exhaustion was checked locally, not by exhausting the live account. | No. These checks do not open the autonomous activation gate. |
| Conversation and controller SQLite persistence | Yes. Saved agent/conversation IDs and owner chat history. | Yes. Restart and continuity tests. | Yes. Persistent controller database and Letta storage are in use. | Yes. SQLite integrity passed, agent/conversation IDs stayed the same, saved record counts did not decrease, the Telegram phrase remained in history, and Letta recalled its separate test phrase after both services restarted. | No. |
| Diary, personality history and encrypted memory backups | Yes. Controller source includes these features. | Yes. Diary, append-only memory, backup encryption and restore coverage. | Included in the deployed controller. Separate backup key loads successfully; startup logged an encrypted snapshot. | Partial. Record counts survived restart and snapshot creation was observed. Live diary delivery, personality changes and snapshot restoration were not exercised. | No. Diary/session behavior remains behind the autonomous gate. |
| Isolated tool runner and filtered egress | Yes, in prototype source. Worker limits and network policy are implemented. | Yes for local component behavior and policy; this does not prove VPS isolation. | No runner/egress service or socket units were found on the VPS. | No complete VPS worker or isolation proof. | No. Shell and browser execution remain disabled. |
| Browser history and site-account primitives | Partial. Encrypted history, exact-origin policy and storage primitives exist; trusted browser control and real account workflows remain unfinished. | Yes for implemented storage and policy behavior. | Controller history storage and its independent key are initialized; browser worker execution is disabled. | No live browser or account workflow was exercised. | No. |
| X, Gmail and other social/email actions | No current integration implementation was found in prototype source; the project description places these in a later owner-opt-in phase. | No integration tests. | No. | No. | No. |
| Autonomous sessions and schedules | Yes for conversation/memory only. A scoped source gate and explicit owner opt-in replace the blanket all-phases gate. External-runtime autonomy is rejected. | Yes. Dedicated lifecycle tests cover wake deduplication, DST, owner pause, sleep, diary, recovery, budgets and hard limits. | `a37e2fb` installed; controller stopped and flags disabled after failed commissioning. Exact controller-tool permission rules await installation. | Startup passed. Initial attempt hid lifecycle tools; retry exposed them, but headless approval rejected their execution. Corrected two-stage retry is pending. | No. The owner has authorized this limited release, but successful commissioning is still required; see the [lifecycle guide](DAILY_LIFECYCLE.md). |

## Verification evidence

### Local checks

```sh
PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -q
python3 -m unittest discover -s scripts/tests -q
bash -n scripts/admin-setup.sh scripts/validate-and-start-controller.sh
systemd-analyze verify systemd/andrew-control.service
```

Before the lifecycle changes, the runtime suite ran **113 tests: 111 passed and 2 were gated/skipped**; the administrator suite passed **5 tests**. The new local lifecycle verification is recorded below. Administrator tests stay outside the runtime suite copied by `activate-phase1.sh`.

The two default skipped tests require explicit live opt-in. A separate read-only App Server capability test passed, and a dedicated live probe exercised Qwen conversation/restart behavior described below. Default-suite skips are not used as evidence that live behavior passed.

### Daily-lifecycle changes: local checks

The updated runtime suite ran **133 tests: 131 passed and 2 gated/skipped**. This includes **20 dedicated lifecycle/policy tests** and regression assertions for controller-tool visibility and exact permissions. The administrator suite passed **9 tests**, including retry-ledger preservation, owner-control rejection and safe failure logging, giving **142 total: 140 passed and 2 skipped**. Shell syntax, Python compilation of the live probe, `git diff --check`, and controller unit verification also passed. A code reviewer checked the activation and rollback paths; its findings were corrected and reviewed again.

The fixes preserve owner pause across wakes, cancel the old autonomous session on manual wake, reject late mutations after sleep, require a daily diary before voluntary autonomous sleep, protect fallback deduplication across clock rollback, rest interrupted sessions and share validated spending settings between the controller and gateway. Local tests substitute the provider and Telegram boundaries. **Both live attempts failed safely before activation. The latest exact permission rules have not yet been exercised live.**

### VPS and live checks

- Both `andrew-letta.service` and `andrew-control.service` were active with zero automatic restarts after the successful probe. Startup confirmed provider/agent initialization, gateway setup, Telegram authentication and the persistent database, followed by Telegram polling.
- The administrator verified independent backup/history credentials, installed the current service unit and compared every installed controller module with its source. The unprivileged shell cannot directly inspect the protected runtime directory; an earlier claim that it was absent was a permission-check error.
- The owner received a real model reply containing the supplied Telegram phrase. The administrator probe checked both message roles in SQLite without printing chat contents.
- The probe stopped the controller, restarted Letta, redeployed and started the controller, checked fresh service invocations, and verified SQLite integrity, stable agent/conversation IDs and preserved records. A Qwen turn then recalled the earlier phrase from the same dedicated Letta conversation.
- New recall charge: **$0.000176072**, **2,289 input tokens**, **155 output tokens**, **1 settled row**, **0 unsettled reservations**. At the end of the probe the ledger held **4 settled rows totaling $0.000668182**. Earlier rows were unchanged after restart. These are gateway-settled records, not an independent comparison with the provider's invoice.
- Root probe results were saved in `/tmp/andrew-live-verification-result.json`; the probe and test marker are temporary verification artifacts outside Git. No credentials or message bodies are included in the result.

## Startup fixes verified

The fixes provision missing independent data keys without replacing existing keys or abandoning protected ciphertext, synchronize the deployed unit and modules, use Letta's WebSocket `agent_update` command, select the registered `openai-compatible` provider, migrate the saved provider handle while preserving IDs, and wait for a refused Letta socket during startup before beginning agent operations.

See [the App Server client](../prototype/andrewbot/appserver.py), [data-key provisioner](../scripts/provision-controller-data-keys.py), [startup installer](../scripts/validate-and-start-controller.sh), and [controller unit](../systemd/andrew-control.service). The administrator helpers target this deployment's fixed `/var/lib/andrew/control` state directory.

## Updating this document

Refresh test and VPS evidence after deployment changes. Claim installed status only after confirming deployed files and services; claim live success only for behavior actually observed. The owner's new request authorizes conversation/memory autonomy, subject to the scoped release gate, opt-in settings, preserved safety/budget gates and successful commissioning. Future external-action phases retain their separate readiness requirements.
