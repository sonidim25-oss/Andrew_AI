# Phase 2: Lifecycle and Autonomous Sessions

## Goal

Add reliable lifecycle handling and bounded autonomous sessions to Andrew while preserving the existing controller, persistent database, Telegram owner controls, and model gateway as the trusted policy boundary.

The owner confirmed `America/Toronto` and a 09:00 daily fallback wake on 2026-10-14. Andrew chooses his own optional work and sleep intervals through persisted lifecycle tools; the 09:00 fallback is the only owner-fixed schedule point. Cost, cycle, pause, and session-length ceilings remain enforced by the controller.

## Current state

The controller already implements `/sleep`, `/wake`, `/wake_at`, `/pause`, queued Telegram messages, a daily fallback wake check, and a persistent SQLite usage ledger. The main loop checks timed and fallback wakes. The fallback checkpoint needs an explicit once-per-local-day rule: if Andrew is awake at 09:00, that day's fallback is consumed so a later sleep does not trigger an immediate late wake. Lifecycle state updates and audit writes are also separate database commits.

Before Phase 2 implementation, there was no autonomous session loop, cycle ledger or ceiling, lifecycle startup reconciliation, or restart recovery. Existing usage accounting measured API cost/tokens but did not count agent cycles. The implementation adds a 150-cycle daily ceiling and $0.30 daily budget target; unused daily allowance may roll forward within the calendar month, subject to the $10 hard cap and $1 protected reserve.

## Design

### Trusted lifecycle controller

The controller remains the sole owner of sleep, awake, paused, and scheduled-wake state. Store state transitions, wake request changes, and corresponding audit events transactionally in SQLite. Persist each received Telegram update in a durable inbox before advancing the Telegram polling offset; acknowledge it only after handling, and recover pending inbox entries after a crash. Use UTC timestamps for persisted deadlines and `America/Toronto` local dates/times for owner-facing daily schedules. A scheduled wake survives process restarts and is evaluated immediately after startup. Repeated checks, clock changes, and DST transitions must not create duplicate wake events or skip a valid local daily wake.

Manual `/wake` clears sleep and pause and releases queued messages for normal budget-gated handling. `/pause` blocks autonomous and queued-message model calls; owner commands remain available. `/sleep` and `/wake_at` preserve the requested wake across restarts. Queued messages are deduplicated using Telegram update IDs. Before replay, the controller claims messages durably; a crash or uncertain model result holds them to prevent automatic duplicate calls. Owner `/replay` explicitly retries held messages and warns that a duplicate answer is possible.

The 09:00 Toronto fallback wake is implemented as a controller schedule, with no independent timer required while the controller's systemd service is running. It wakes indefinite sleep and starts an optional session when Andrew is not paused and budget remains; explicit timed sleep is honored. At the end of each turn, Andrew may continue, sleep for 1–1440 minutes, or request a persisted local wake time. The controller enforces the hard session ceiling regardless of his choice. The first live session is observed for 5–10 minutes before unattended daily operation, as a validation checkpoint rather than a fixed schedule requirement.

### Autonomous sessions and cycle accounting

Add an explicit session runner owned by the trusted controller. A session is created only when awake, not paused, and within spend and cycle ceilings. A daily fallback wake or persisted agent-requested wake may start an optional session; the agent may request multiple sessions per day by choosing a later wake time after sleeping. There are no fixed work hours. A session has a persisted start time, session ID, status, and audit events. One completed cycle is one Letta work turn, including its controller-mediated tool calls; retries, startup checks, rejected requests, and Telegram owner commands are not cycles. The 150/day safety ceiling counts started turns, including failed or interrupted turns, so a crash cannot provide extra attempts; completed, failed, and interrupted outcomes remain distinguishable in the ledger. Persist cycle records separately from API usage records so the two measures remain independently inspectable.

Initial ceilings are a maximum 150 autonomous cycles per Toronto calendar day and 180 minutes per session. The 100–150 cycles/day figure remains a soft desired range, not a target that overrides cost or time limits. There is no mandatory session duration. Manual owner wake permits an interactive session under the normal monetary budget and does not grant extra API spend; it is tracked separately from autonomous cycles. The runner stops on sleep, pause, explicit stop, time ceiling, cycle ceiling, budget gate, or unrecoverable service/provider failure.

Do not start a live autonomous session or enable unattended daily operation during Phase 2. Defer both until all project phases are complete, as requested by the owner. Runtime environment flags alone must not activate sessions: the source release gate remains closed until all phases are complete and reviewed. At that point, first observe one 5–10-minute run after waking Andrew, allowing him to choose his own next lifecycle action; review cost, cycle count, stop behavior, and audit events before leaving unattended scheduling active. No shell, browser, social, email, or other worker capability is enabled by this phase; those remain unavailable until their isolation work is complete.

### Budget and rollover

Keep API cost and token usage in the existing usage ledger. Add a separate cycle ledger. The budget gateway remains the final enforcement point for every provider request and reserves the maximum possible request cost before dispatch, then reconciles provider usage. Calendar-month spend may not exceed $10. The $1 reserve is unavailable to ordinary cycles; it may be used only for bounded session wrap-up after new autonomous work has stopped. Daily allowance accrues at $0.30 per Toronto calendar day, and unused allowance rolls forward only within that calendar month. Available spend is always bounded by both accumulated daily allowance and monthly spend remaining after the protected reserve. Monthly reset clears the daily rollover pool and cycle count; it does not erase historical ledger records.

If provider usage cannot be reconciled, fail closed using the reserved maximum cost. If the ledger is unavailable, no new model request or autonomous cycle starts. Daily cycle count and allowance reset at the Toronto calendar-day boundary; the monetary rollover pool resets at the calendar-month boundary. Owner-facing status reports current lifecycle state, next scheduled wake, today's autonomous cycles, today's spend/allowance, monthly spend, and reserve without exposing secrets.

## Recovery and audit

At startup, load persisted state before processing queued input. Expired timed wakes and missed fallback wakes are evaluated once and logged once. An interrupted autonomous session is marked interrupted; it is not silently continued or counted as a completed cycle. A fresh cycle may start only after normal lifecycle and budget gates pass. Recovery never duplicates a provider call based on an uncertain in-flight result; its reserved cost remains charged conservatively.

Audit records cover owner and agent lifecycle requests, wake completion, session start/stop/interruption, cycle completion, budget denial, queued-message release, and recovery. Records contain event IDs and safe summaries, not secrets or hidden reasoning.

## Validation

Before any live activation, verify transactional state/audit consistency; timed and fallback wake behavior across restarts, repeated polling, and DST boundaries; owner command semantics; queued-message deduplication and retry; cycle counts and session limits; daily rollover and month reset; monthly reserve and hard-cap enforcement; fail-closed behavior; and safe recovery from interrupted/in-flight work. Defer the manually observed 5–10-minute live session and unattended schedule activation until all project phases are complete.

## Out of scope

This phase does not enable shell/browser workers, external social/email actions, the daily diary, model switching, or a long unsupervised trial. Those belong to later phases or require the separate isolation and owner-review gates already described in the project plan.
