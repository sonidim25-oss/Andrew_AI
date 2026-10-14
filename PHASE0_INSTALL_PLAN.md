# Phase 0 Findings and Proposed Install Plan

**Date:** 2026-10-13
**Status:** Phase 0 complete; awaiting owner review before any system changes.

## Decision requested

Review [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) and [PHASE1_IMPLEMENTATION_PLAN.md](PHASE1_IMPLEMENTATION_PLAN.md), then approve, reject, or revise the plan before Phase 1. No packages, services, containers, users, timers, ports, volumes, firewall rules, credentials, or existing production configurations were changed during this review.

## Proposed architecture

Use Letta for durable agent state and memory. Build a small trusted controller for lifecycle, scheduling, wake-request approval, model gateway, budget enforcement, Telegram, event logging, redaction, backup and kill switch. Use a rootless Podman tool runtime with a networkless worker container, a dedicated workspace, and a validated Unix-socket egress proxy. Keep provider and Telegram secrets in an encrypted credential vault available only to trusted controller services. Persist Letta state, diary, structured events, personality history and project files in separate versioned/backup-managed data paths.

The controller should own the initial immutable founding-letter event and deliver it once before the first autonomous activity. The letter remains an autobiographical record, never the source of safety policy. Phase 1 retains owner-provided credentials in the encrypted vault and permits only the trusted controller to use Telegram and the model provider. The agent receives no raw credentials; X and email actions, registration, posting, direct messages, payments and external address-book interactions stay disabled until a later owner-opt-in phase.

## Phased work after approval

### Phase 1 — isolated persistent runtime

1. Before implementation, obtain explicit confirmation of [PHASE1_IMPLEMENTATION_PLAN.md](PHASE1_IMPLEMENTATION_PLAN.md). The owner has selected calendar-month accounting and Qwen3.8 Flash first. Confirm the configured timezone and fallback time before enabling the later scheduler; obtain the Telegram numeric owner ID before enabling polling or administrative commands.
2. Recheck host resources, container state and firewall policy, then create the dedicated unprivileged service accounts and rootless runtime. Install only the approved runtime dependencies. Do not alter existing website or bot containers, their networks, volumes, firewall rules or reverse proxy.
3. Start Letta with private-only networking and durable state. Pin the tested Letta image/version and database format; use `pgvector` only if the selected backend requires PostgreSQL.
4. Implement the trusted controller and separate tool worker. Add controller-owned external tools with strict input validation, per-action logging and no generic host shell endpoint.
5. Configure the networkless tool worker and egress proxy. Prove blocking of host loopback, private/link-local ranges, Docker networks, cloud metadata, direct DNS and direct-IP requests. Prove no host secrets or Docker API are readable.
6. Run controlled proofs for one model call, one sandboxed shell action, memory write/read, restart continuity, model switch, cost accounting and kill switch. Use a hard test allowance below the initial $10 cap.
7. Import owner-provided credentials by interactive local SSH prompt with terminal echo disabled. Store Telegram, OpenRouter, email and X records in the encrypted vault. Only the trusted controller may use the Telegram token and model key; X and email use remain disabled in Phase 1. Provide backup and rollback steps, then report evidence and residual risks before proceeding to Phase 2.

### Phase 2 — lifecycle and autonomous sessions

Implement trusted state transitions, daily fallback wake, persisted agent-requested wake times, owner manual wake/sleep/pause, queued sleeping messages, deduplication, timezone/DST behavior, crash recovery and separate cycle versus API-usage accounting. Use the owner-approved calendar-month and daily policy. Evaluate `qwen/qwen3.8-flash` first, then compare the approved Gemini Flash candidate with metered usage. Run one manually observed 5–10 minute session before enabling any schedule.

### Phase 3 — Telegram and diary

Complete the owner-only Telegram views and diary pipeline in the trusted controller. `/diary` shows the latest immutable first-person entry, `/diary YYYY-MM-DD` selects an earlier date, and `/diary retry YYYY-MM-DD` explicitly retries an uncertain send with a duplicate warning. Keep diary content separate from chat, cycle, spend, and objective activity records. A controller-authored, clearly labeled incident summary may be sent after an interrupted session; never fabricate a first-person diary. Apply content checks for secret patterns and private-reasoning markers. Diary delivery remains dormant until an entry is saved, and Phase 2's all-phases activation gate remains closed.

### Phase 4 — memory/personality continuity

**Workspace implementation complete (2026-10-14); controller deployment and live Letta model-switch validation remain pending.** The prototype stores append-only personality revisions and dated experience/interest records in controller SQLite, searches them through FTS5, injects the current personality as ephemeral model context, and exposes bounded controller memory tools. Existing Phase 3 diary rows share the same persistent database and are included in encrypted snapshots. Local AES-GCM snapshots run at service startup and every 24 hours, retain seven authenticated copies, and have a root-operated snapshot/restore command using a separate systemd encrypted key. Owner-managed model ID and token-rate settings are shared across the provider, gateway, controller, and Letta agent; a configured change updates an existing Letta agent while preserving SQLite memory.

Offline tests cover append-only history, reference validation, search, restart continuity, snapshot encryption/restore, model configuration, and the mocked Letta update request. Two live Letta tests remain skipped; the owner-configured model change has not been sent to the running App Server. No controller service, autonomous session, or scheduled wake was started. Backups are local only and do not protect against host or systemd host-key loss. See [the Phase 4 design](docs/superpowers/specs/2026-10-14-phase4-memory-continuity-design.md), [implementation plan](docs/superpowers/plans/2026-10-14-phase4-memory-continuity.md), and [backup operations](docs/memory-backup-operations.md).

### Phase 5 — monitored live trial

After all other project phases are complete and the owner reviews the evidence, perform one observed short session, then enable the owner-configured fallback wake and Andrew-directed sessions. The owner has asked to defer live sessions and unattended scheduling until all phases are complete. Begin below the full $10/month cap, observe usage and resource impact, and raise the cap only if the owner approves. No fixed session timetable is introduced. Phase 6 remains a separate owner-opt-in project.

### Phase 6 — separate opt-in social identity work

Treat browser identity, encrypted credential brokerage, email reading, registrations, messaging, posting and the persistent contacts/address book as a separate owner-approved project. Review each target site's current rules and the owner's permission settings first. Keep all outward communication disabled until each capability is explicitly enabled.

## Initial resource limits for Phase 1

Start with the review's envelope: 2 vCPU/3 GiB RAM/10 GiB persistent state for Letta/controller/database, plus 2 vCPU/2 GiB RAM/10 GiB workspace for one tool/browser worker, with a 256-process cap and bounded command duration. Keep at least 4 GiB RAM available for the host and existing services. These are initial engineering ceilings; measure the 5–10 minute trial and adjust before any daily schedule.

## Rollback outline

Disable only the new agent controller/timer and stop only the new rootless-runtime services. Preserve the agent database, workspace and logs for inspection. Remove new isolated service files and data only after a separate explicit retention decision. Existing production service files, Docker daemon, containers, ports, networks, volumes, firewall and reverse proxy remain outside the rollout and rollback scope.

## Required owner settings

- Timezone and daily fallback wake time: private deployment configuration. Activation remains deferred until all project phases are complete.
- Telegram owner numeric user ID is needed before enabling privileged bot commands.
- OpenRouter/model use and all credential imports begin only after explicit Phase 1 confirmation; the key values stay out of this plan and source control.
