# Phase 4: Memory and Personality Continuity

**Date:** 2026-10-14
**Status:** Design approved by owner; implementation complete in the workspace prototype, pending live deployment validation.

## Goal

Give Andrew versioned, durable autobiographical and personality memory that can be retrieved when relevant, survives restarts, and remains available across an owner-managed model change. Preserve immutable historical records, log every edit, and create encrypted backups of the durable records.

## Approved architecture

SQLite in the trusted controller is the canonical store for memory records, personality revisions, event references, and audit history. Letta receives a concise current-personality projection and only relevant retrieved records as context. Letta memory is a projection, not the canonical archive.

The controller owns the memory storage and retrieval interface. This keeps the record format and retrieval behavior available when the configured model changes. The configured model remains an owner-managed server setting; a model change does not migrate or rewrite memory.

## Memory records and revisions

Keep separate record types for:

- **Personality revisions:** dated statements about evolving personality, interests, beliefs, relationships, goals, and projects.
- **Experiences:** dated autobiographical records, each linked to one or more supporting event or diary IDs when available.
- **Diary records:** the immutable first-person entries already defined by Phase 3. Phase 4 includes them in durable storage and backups but does not redefine diary authorship, delivery, or editing behavior.
- **Immutable owner records:** the founding letter and operational safety constraints remain outside agent-editable memory. Preserve the founding letter exactly as authored.

Each personality edit creates a new revision rather than overwriting the prior text. Store the creation timestamp, effective date, author/source (Andrew or owner), a concise reason, and references to the supporting event or diary records. Keep previous revisions queryable. Every accepted edit and rejected edit attempt is recorded in the objective audit log without copying sensitive content into ordinary logs.

Andrew may create experience and interest records during normal sessions. Personality changes are recorded as dated revisions with an explanation and supporting reference. Memory tools cannot change safety constraints, owner controls, spending limits, or the founding letter.

## Retrieval and model context

At session start, the controller renders the current personality revision as a compact context projection. During a session, Andrew can request relevant memory through a controller-owned search interface. Search results include record IDs, dates, record type, text, and provenance references so the model can distinguish recorded facts from interpretations.

Retrieval is scoped by query relevance and recency and returns a bounded number of records. The controller does not load the full archive into every prompt. Retrieved records are read-only in the model context; changes must go through the validated memory write interface, which creates a new record or revision and audit entry.

## Durable storage and encrypted backups

Store canonical memory and existing Phase 3 diary records in the controller's persistent SQLite database, using additive schema migrations that preserve existing rows. Create encrypted, rotating local snapshots of that database. A restore check must verify that the database opens and that memory revision history and diary records remain present and readable.

Off-host backup delivery is out of scope for this phase unless separately specified. Local encrypted snapshots protect recovery from database corruption or mistaken deletion, but do not by themselves provide recovery after loss of the host. Backup-key recovery must be documented before implementation; the credential vault key must not be silently assumed to be an appropriate backup key.

## Model change

Model changes are made by the owner through server configuration. On a configured model change, the controller supplies the same current personality projection and uses the same SQLite retrieval interface. The change must not reset memory, rewrite history, or expose provider credentials to Andrew. No Telegram model-selection command is added in this phase.

## Audit, privacy, and limits

- Historical personality revisions, diary records, and experience records are append-only; corrections create new dated records or revisions.
- Record provenance and distinguish verified events from Andrew's interpretations.
- Keep objective audit events, subjective diary records, and personality/experience memory in distinct logical record types.
- Never put credentials, API keys, private reasoning traces, or raw secret values in memory records, backups' filenames, or routine logs.
- Apply bounded record size and bounded retrieval results so memory cannot grow each prompt without limit.
- No new shell, browser, social, email, or external communication capability is introduced.
- This phase does not enable the controller service, autonomous sessions, or scheduled wakes.

## Acceptance criteria

1. Creating a personality edit produces a new dated revision with reason, source, and event or diary references; earlier revisions remain unchanged and retrievable.
2. Experience and interest records can be stored with provenance and retrieved by relevance without loading the full archive into context.
3. Every accepted edit and rejected edit attempt has an objective audit entry that does not contain secret values.
4. The current personality projection and relevant retrieval results are supplied through the controller's Letta integration, while the canonical records remain in SQLite.
5. A restart preserves memory records, revision history, diary records, and retrieval behavior.
6. Encrypted rotating local snapshots can be restored and verified without losing memory revisions or diary records; backup-key recovery steps are documented.
7. An owner-configured model change preserves and retrieves the same records without rewriting history or disclosing credentials.
8. Safety constraints and the founding letter cannot be edited through memory tools.
9. No autonomous session, scheduled wake, controller service, or external communication is enabled by Phase 4 implementation.

## Out of scope

- Enabling live or unattended autonomous sessions; Phase 5's monitored trial remains gated until all phases are complete and reviewed.
- Off-host backup delivery or third-party storage.
- Owner-facing memory management commands or UI.
- Social address book implementation, account access, email, posting, or messaging; those belong to later owner-approved work.
- Replacing Letta or migrating the existing diary pipeline.

## Self-review

- The design identifies one canonical store (controller SQLite) and one derived model-context projection (Letta), avoiding ambiguous ownership of memory state.
- Revision history, provenance, immutability boundaries, and the relationship to Phase 3 diary records are explicit.
- The backup design states its local-only recovery limit and leaves key recovery documentation as an implementation requirement.
- Model changes remain owner-managed configuration changes, as approved in conversation.
- Phase 4 remains dormant capability work and cannot activate the service or schedules.
