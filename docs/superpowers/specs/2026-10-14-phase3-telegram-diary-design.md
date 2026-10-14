# Phase 3: Telegram Diary and Owner Views

## Goal

Give the owner reliable Telegram access to Andrew's dated first-person diary and objective activity records, while keeping those two records separate and preventing private reasoning or credentials from reaching Telegram.

## Confirmed constraints

- The dedicated Telegram bot and numeric owner-ID check already exist. Non-owner, forwarded, group, and non-private messages do not receive privileged handling.
- `/status`, `/wake`, `/sleep`, `/wake_at`, `/pause`, `/replay`, `/activity`, `/budget`, and `/help` already exist. `/diary` currently shows recent chat history and must be repurposed.
- Andrew's diary must be first person and subjective. It may describe reflections, discoveries, setbacks, and plans, but must not claim unverified events as facts or expose private chain-of-thought.
- Objective lifecycle, budget, and system events remain in the activity/audit log. A diary entry may reference event IDs; it does not replace or modify them.
- Autonomous sessions and daily fallback remain source-gated until all project phases are complete and reviewed. This phase adds dormant capability only; it does not activate sessions, schedules, or a service.
- The owner requested Git later; no commit is part of this phase.

## Design

### Diary capture and storage

Add an agent-invoked controller tool, `save_diary_entry`, for submitting a finished entry before voluntary session sleep. The controller accepts plain text plus optional objective activity-event IDs, associates the configured `America/Toronto` local date and current session ID, and stores it as an immutable diary record. There is at most one entry per Toronto-local calendar date. Entries are capped at 3,000 Unicode characters; invalid or over-limit submissions fail without changing lifecycle state. Diary content is not copied from conversation history or the objective activity feed.

The tool instructions ask Andrew to write a concise, shareable first-person account of his own observations and plans, distinguish inference from verified events, include event references only when relevant, and omit credentials, secrets, and private reasoning traces. A read-only `recent_activity` controller tool exposes up to eight objective event IDs and safe summaries, without chat or diary text, so Andrew can cite only verifiable event IDs. The trusted controller validates event references against its own activity ledger and applies a secret-pattern scan before storage. Unsafe entries are rejected and are not delivered. Diary records are append-only; corrections are not edits to old entries and require a later dated entry.

### Delivery and owner commands

Persist Telegram delivery state with the diary record so a restart can find entries not yet delivered. Deliver only after storage commits. Mark delivery complete after Telegram confirms success. If delivery outcome is uncertain, retain an explicit uncertain state for the owner to inspect; do not silently claim delivery. Telegram's send method has no idempotency key, so a retry after an uncertain response may duplicate a diary message and must be explicit.

`/diary` displays the newest stored diary entry, and `/diary YYYY-MM-DD` displays that Toronto-local date's entry. `/diary retry YYYY-MM-DD` is the explicit owner action for an uncertain delivery and warns that it can duplicate a message. `/diary` reports clearly when no entry exists. `/activity` continues to show objective audit events only. `/status` may report the latest diary date and whether its delivery is pending or uncertain, without including diary text. All output remains owner-only, private-chat-only, and safe to split within Telegram message size limits.

### Session endings and incident summaries

Andrew is prompted to save a diary before voluntarily sleeping or ending an active session. If an active session stops abnormally before an entry exists, the controller stores and sends a clearly labeled, automatically generated incident summary based only on known lifecycle and audit facts. It is never written in Andrew's voice and does not invent thoughts or outcomes. Normal hard limits and budget denials do not authorize another model request just to create a diary; no additional diary-only model call is made in Phase 3.

### Compatibility and safety

Keep diary rows separate from `messages`, `activity`, cycle, and spend records. Add a SQLite migration that preserves existing rows and does not require credential migration. Telegram ingress remains durably queued before polling advances, as implemented in Phase 2. No shell/browser/social/email capability is added. Logging records diary IDs and state transitions only, never entry contents, secrets, or model reasoning.

## Data contract

Each diary record contains a generated ID, Toronto-local date, UTC creation timestamp, optional autonomous session ID, content, zero or more validated activity-event IDs, record type (`diary` or `incident_summary`), and delivery state (`pending`, `delivered`, or `uncertain`). Normal diary records are immutable and unique by local date. Incident summaries are system-authored and labeled; they do not masquerade as diary entries.

## Out of scope

- Enabling autonomous sessions, the daily fallback, or the controller service.
- Editing personality or long-term memory, which belongs to Phase 4.
- External messages beyond the configured owner Telegram chat.
- Arbitrary chat-history export, diary deletion UI, or new retention/rotation policy.
- Tests that require a live Telegram token or a live autonomous session.

## Acceptance criteria

1. `/diary` and `/diary YYYY-MM-DD` return stored diary records only; empty dates are reported plainly.
2. A valid `save_diary_entry` call creates one immutable entry for the Toronto-local date and validates each referenced event ID.
3. Duplicate daily entries, unknown event references, invalid text, over-limit text, or secret-pattern matches are rejected without partial writes or lifecycle changes.
4. Entries are persisted before Telegram delivery; successful sends become delivered, and uncertain sends remain explicitly uncertain across restart.
5. An abnormal session stop before diary creation produces a labeled, factual incident summary, never first-person invented content.
6. `/activity` remains objective, owner authorization remains unchanged, existing SQLite data survives migration, and no runtime flag can bypass the Phase 2 activation gate.

## Self-review

- Scope is one diary pipeline integrated with the existing Telegram command path, session runner, and SQLite store; no independent social or personality subsystem is included.
- The one-entry-per-local-day rule, size limit, command date format, and delivery states are explicit.
- Diary content, event facts, and private reasoning have distinct handling rules.
- Uncertain Telegram sends are not silently retried, and abnormal sessions do not trigger an extra paid model call.
