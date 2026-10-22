# Owner-configured model change between settled turns

## Incident — 2026-10-22 02:23 UTC

`/home/daniel/switch-andrew-luna-20261022.py` (owner-run, paused) passed its
synthetic Luna preflight, wrote `/etc/andrew/model.conf` and restarted the
controller. Startup failed with `Letta model update (LettaCommandError:
unsupported_contract)` and the script restored Qwen3.8 Flash. No database change.

Cause: once the Letta agent is adopted by journaled recovery,
`guardLegacy` (`letta_recovery/adoption.mjs`) refuses every legacy mutation;
`agent_update` was allowed only as an exact repeat of stored settings. The
controller's `update_agent_model` changes `model`, so it was refused.

## Change

The patch was applied by the owner (`/home/daniel/andrew-model-switch-guard.patch`)
because the agent's safety check does not let it relax a protection itself.

`agent_update` on an adopted agent now reaches Letta's ordinary handler only when:

- the body contains `model` and at most `model_settings`; `model` is an
  `openai-compatible/…` handle (no `..`) different from the current model;
  `model_settings`, if present, equals the current settings exactly. Prompt,
  tools, memory and every other field stay frozen.
- every journaled checkpoint of that agent is `terminal_committed`,
  acknowledged and settled with no claim, and Letta has no active run.

Otherwise it is refused exactly as before. Exact repeats remain no-ops. The
controller already includes the model in each turn's configuration digest, so
new turns get a new `config_sha`; there is no held turn left to resume.

## Tests

- Node (`adoption.test.mjs`): refused while input committed, while terminal is
  unacknowledged, and while the claim is held; allowed after ack and release;
  refused for prompt/tools/settings changes, other providers, path escapes,
  non-string models, arrays, and while any legacy run is active; repeats stay
  no-ops. Fails without the patch. 88 backend tests pass.
- Real patched backend + real controller startup (`test_local_recovery_crash_protocol.py`):
  a completed turn, then restart with `openai/gpt-6-luna`: next turn completes,
  every new provider call uses Luna, state records the new model and handle, and
  two configuration digests exist. With a held turn, the same restart fails at
  `Letta model update` with no provider calls and the old model kept. The success
  test fails against the installed backend `67b570aa…`.

## Rollout

Backend-only (controller files unchanged, so no manifest binding). Backend
builds are reproducible: a local build of `c24bd8e` hashed to the installed
`67b570aa…`; this change builds to `7d78ea00…`.

1. `/pause`
2. `sudo /home/daniel/deploy-andrew-model-switch-20261022.sh`
3. `sudo python3 /home/daniel/switch-andrew-luna-20261022.py` (expected backend
   updated to `7d78ea00…`; it keeps its own preflight, backup and rollback).
4. `/wake`.
