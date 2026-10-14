# Phase 4 Memory and Personality Continuity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add versioned, auditable autobiographical memory with bounded relevant retrieval, restart/model continuity, and encrypted local backups.

**Architecture:** The trusted controller's SQLite database is the canonical store for personality revisions and experience records. A focused memory module provides validated writes, FTS5-backed retrieval, and a compact current-personality projection to the Letta agent. A separate backup module snapshots SQLite consistently, encrypts the snapshot with a dedicated systemd credential, and verifies restores without changing the live database.

**Tech Stack:** Python 3.12, SQLite 3.45 with FTS5, existing Letta App Server WebSocket client, existing `cryptography` AES-GCM dependency, systemd encrypted credentials, and `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-14-phase4-memory-continuity-design.md`

## Global Constraints

- SQLite in the trusted controller is the canonical store for memory records, personality revisions, event references, and audit history.
- Letta receives a concise current-personality projection and only relevant retrieved records as context.
- Each personality edit creates a new revision rather than overwriting the prior text.
- Every accepted edit and rejected edit attempt is recorded in the objective audit log without copying sensitive content into ordinary logs.
- The founding letter and operational safety constraints remain outside agent-editable memory.
- Create encrypted, rotating local snapshots of the database; off-host backup delivery is out of scope.
- Model changes are made by the owner through server configuration; they must not reset memory or expose provider credentials.
- Do not enable the controller service, autonomous sessions, scheduled wakes, or external communication.
- Do not commit; the owner deferred Git.

## Review Focus

- Empty, malformed, very long, and secret-like writes: reject safely without storing content or changing state (Task 2 tests validation and audit behavior).
- Concurrent personality edits: retain every accepted revision and do not silently overwrite the current revision (Task 1 tests revision ordering and transaction behavior).
- Unknown, quoted, or punctuation-only search queries: return a bounded safe result or an empty result without raising SQL/FTS syntax errors (Task 2 tests query normalization).
- Corrupt, truncated, or wrong-key backup files: refuse restore and leave the live database untouched (Task 4 tests failure atomicity).
- Invalid model IDs or inconsistent model/rate configuration: fail closed before provider requests or Letta initialization (Task 5 tests startup validation).

---

### Task 1: Add versioned memory records to SQLite

**Files:**
- Modify: `prototype/andrewbot/store.py`
- Create: `prototype/tests/test_memory_store.py`

**Interfaces:**
- Produces `Store.create_memory_record(record_type: str, content: str, source: str, event_ids: list[int] = (), diary_ids: list[str] = ()) -> str` for `experience` and `interest` records.
- Produces `Store.create_personality_revision(content: str, reason: str, source: str, event_ids: list[int] = (), diary_ids: list[str] = ()) -> str`; each call appends a new immutable revision.
- Produces `Store.current_personality_revision() -> dict | None`, `Store.personality_revisions(limit: int = 20) -> list[dict]`, and `Store.get_memory_record(record_id: str) -> dict | None`.
- Produces `Store.search_memory_rows(match_query: str, limit: int = 8) -> list[dict]`, using FTS5 BM25 ranking plus recency ordering and returning record IDs, dates, types, text, source, and event/diary references.
- Creates additive `memory_records`, `personality_revisions`, reference tables, and FTS5 index. Existing `diary_entries`, activity, lifecycle, usage, and message rows remain intact.

- [x] **Step 1: Write failing store tests** `test_memory_schema_migrates_existing_database`, `test_personality_revision_is_append_only_and_ordered`, `test_memory_records_validate_event_and_diary_references_atomically`, `test_memory_search_returns_bounded_ranked_rows`, and `test_memory_writes_create_audit_events_without_content`.
- [x] **Step 2: Run `PYTHONPATH=prototype python3 -m unittest prototype.tests.test_memory_store -v`** and confirm the new APIs/schema are missing.
- [x] **Step 3: Implement additive schema and transactional store methods** in `prototype/andrewbot/store.py`. Enforce FTS5 availability at initialization, immutable inserts, validated references to `activity.id` and `diary_entries.entry_id`, and objective audit messages containing IDs and operation outcomes only.
- [x] **Step 4: Run `PYTHONPATH=prototype python3 -m unittest prototype.tests.test_memory_store -v`** and confirm migration preserves existing tables and all new store tests pass.

### Task 2: Validate memory writes and expose a memory service

**Files:**
- Create: `prototype/andrewbot/memory.py`
- Create: `prototype/tests/test_memory.py`
- Modify: `prototype/andrewbot/controller.py`

**Interfaces:**
- Produces `validate_memory_text(value: object, maximum: int = 4000) -> str`, rejecting empty, non-string, over-limit, credential-like, and private-reasoning-marker content with fixed safe errors.
- Produces `normalize_memory_query(query: object) -> str`, reducing input to safe FTS5 terms and returning an empty normalized query when no searchable terms remain.
- Produces `MemoryService(store).propose_personality_edit(content: object, reason: object, event_ids: object = (), diary_ids: object = ()) -> str`, `add_experience(content: object, record_type: str, event_ids: object = (), diary_ids: object = ()) -> str`, `search(query: object, limit: int = 8) -> list[dict]`, and `current_personality_context() -> str`.
- Adds controller tool routes `memory_search`, `memory_add_experience`, and `memory_propose_personality_edit`; each write returns a record/revision ID and a concise accepted/rejected result, never prior revision contents or arbitrary DB errors.

- [x] **Step 1: Write failing validation and service tests** `test_memory_text_rejects_empty_secret_and_reasoning_markers`, `test_overlong_write_is_rejected_without_storage`, `test_query_normalization_handles_fts_operators_quotes_and_empty_text`, `test_memory_search_is_bounded`, `test_personality_edit_records_source_reason_and_references`, and `test_rejected_edit_is_audited_without_raw_content`.
- [x] **Step 2: Run `PYTHONPATH=prototype python3 -m unittest prototype.tests.test_memory -v`** and confirm failures on the missing service/API.
- [x] **Step 3: Implement validation and service methods** with an 8-result default cap and an absolute limit of 20. Source is `andrew` for agent tool calls; owner source is reserved for a future owner interface.
- [x] **Step 4: Add strict external tool schemas and controller routing** in `controller.py`; do not add filesystem, shell, browser, Telegram send, or safety-policy mutation tools.
- [x] **Step 5: Run `PYTHONPATH=prototype python3 -m unittest prototype.tests.test_memory -v`** and confirm safe success and rejection behavior. This file exercises the new controller routes; the legacy controller suite currently has unrelated budget and wake-time failures recorded in the execution ledger.

### Task 3: Supply current and retrieved memory to Letta

**Files:**
- Modify: `prototype/andrewbot/appserver.py`
- Modify: `prototype/andrewbot/gateway.py`
- Modify: `prototype/andrewbot/main.py`
- Modify: `prototype/tests/test_appserver.py`
- Modify: `prototype/tests/test_gateway.py`

**Interfaces:**
- Extends `LettaAgent.__init__(..., model_config=None)`; until Task 5 introduces the shared config, `None` preserves the current Qwen default.
- `ModelGateway` accepts an optional `memory_service` and injects the current personality projection as a separate, ephemeral system context in the provider request. It is recomputed for each model call and never appended to the persistent Letta conversation.
- Exposes `memory_search` through the strict external-tool callback; retrieved records are read-only context, while writes go through controller tool calls.
- Updates `LettaAgent.system_prompt` with instructions to distinguish recorded facts from interpretation, search before asserting prior experience when useful, and use memory write tools for dated records/revisions.
- Current personality projection is bounded to 4,000 characters; retrieved content is bounded by Task 2 and the current 64,000-byte gateway input limit. Task 5 passes the same immutable `ModelConfig` to the agent and gateway.

- [x] **Step 1: Write failing App Server and gateway tests** `test_runtime_advertises_memory_tools_without_local_tools`, `test_gateway_injects_current_personality_as_ephemeral_system_context`, `test_memory_search_tool_is_bounded_and_read_only`, and `test_memory_tools_cannot_edit_owner_records`.
- [x] **Step 2: Run `PYTHONPATH=prototype python3 -m unittest prototype.tests.test_appserver prototype.tests.test_gateway -v`** and confirm the new memory tool schemas and gateway context behavior are absent.
- [x] **Step 3: Add strict memory tool schemas and update agent instructions** in `appserver.py`; preserve the founding letter and safety rules in their existing owner-controlled locations.
- [x] **Step 4: Inject the bounded current-personality projection in `ModelGateway`** as a separate system message immediately after existing system messages; leave persistent conversation messages unchanged, and include injected bytes in the gateway's 64,000-byte request limit and budget reservation.
- [x] **Step 5: Wire one `MemoryService` instance through `main.py` to `Controller` and `ModelGateway`** without changing `--validate-only` into a service start or changing `ALL_PROJECT_PHASES_COMPLETE`.
- [x] **Step 6: Run `PYTHONPATH=prototype python3 -m unittest prototype.tests.test_appserver prototype.tests.test_gateway prototype.tests.test_memory -v`** and confirm memory tools and ephemeral context stay within the existing gateway boundary.

### Task 4: Encrypted rotating SQLite snapshots and restore verification

**Files:**
- Create: `prototype/andrewbot/memory_backup.py`
- Create: `prototype/andrewbot/memory_backup_cli.py`
- Create: `prototype/tests/test_memory_backup.py`
- Modify: `prototype/andrewbot/main.py`
- Modify: `scripts/admin-setup.sh`
- Modify: `scripts/validate-and-start-controller.sh`
- Create: `scripts/andrew-memory-backup`
- Create: `docs/memory-backup-operations.md`
- Modify: `systemd/andrew-control.service`
- Modify: `PHASE0_INSTALL_PLAN.md`
- Modify: `PROJECT_DESCRIPTION.md`

**Interfaces:**
- Produces `create_encrypted_snapshot(store: Store, backup_dir: str, key: bytes, retention: int = 7) -> Path` using SQLite's online backup API and AES-256-GCM. Snapshot envelope is versioned, includes a 12-byte nonce, and authenticates the format version as associated data. Retain the newest seven valid snapshots; write atomically with mode `0600` in a mode `0700` directory.
- Produces `restore_snapshot(snapshot_path: str, destination_path: str, key: bytes) -> None`, decrypting to a temporary database, requiring `PRAGMA integrity_check` to return `ok`, requiring memory revision and diary tables to exist, and atomically replacing only the requested destination after validation.
- Provides an admin `andrew-memory-backup snapshot` command and `andrew-memory-backup restore <snapshot-filename>` command. The root wrapper obtains the credential through a one-shot systemd unit; restore refuses while `andrew-control.service` is active and never accepts arbitrary filesystem destinations.
- Loads the dedicated 32-byte backup key through `CREDENTIALS_DIRECTORY/andrew-memory-backup-key`. Provisioning/rotation instructions use a separately named root-owned systemd encrypted credential; do not reuse the credential vault key.
- Creates a snapshot once after normal startup and database migration, then every 24 hours while the controller is running. Skip snapshot creation in `--validate-only` mode. Snapshot errors are logged with a safe category and do not claim success or stop normal Telegram/lifecycle processing.
- Updates `andrew-control.service` and one-shot validation sandbox credentials/read-write paths to provide the backup key and access `/var/lib/andrew/control/backups`. Never start or restart the unit as part of this task.

- [x] **Step 1: Write failing snapshot and restore tests** `test_snapshot_contains_wal_committed_memory_and_diary_rows`, `test_snapshot_is_encrypted_and_restorable`, `test_corrupt_or_wrong_key_restore_leaves_destination_unchanged`, `test_snapshot_rotation_keeps_seven_latest`, `test_snapshot_file_permissions_are_restrictive`, `test_periodic_snapshot_runs_at_start_and_after_24_hours`, `test_validate_only_does_not_create_backup_scheduler`, and `test_admin_cli_creates_and_restores_only_named_local_snapshots`.
- [x] **Step 2: Run `PYTHONPATH=prototype python3 -m unittest prototype.tests.test_memory_backup -v`** and confirm the backup module is missing.
- [x] **Step 3: Implement snapshot, authenticated encryption, restore validation, atomic replacement, and seven-snapshot rotation** using existing `cryptography` dependency and SQLite online backup; never put record content in filenames or logs.
- [x] **Step 4: Add daily snapshot scheduling to `main.py`**, starting only after the validation-only return and running at most once per 24 hours without blocking normal controller processing.
- [x] **Step 5: Add the admin backup CLI and root wrapper** for manual snapshot and restore. Restore must refuse while the controller is active and may select only a file within the configured backup directory.
- [x] **Step 6: Add systemd credential and path configuration plus owner key-generation/recovery instructions**. `admin-setup.sh` creates the dedicated random 32-byte key only when missing and stores it as a systemd host-encrypted credential; document same-host recovery and that local snapshots do not survive host loss.
- [x] **Step 7: Update `validate-and-start-controller.sh` to install every source `prototype/andrewbot/*.py` module with root ownership and mode `0640`, and to provide the encrypted backup credential and backup path in its one-shot sandbox.** Never print key material or start the service in validation-only mode.
- [x] **Step 8: Run `PYTHONPATH=prototype python3 -m unittest prototype.tests.test_memory_backup -v`** and confirm successful restore, failure atomicity, daily scheduling, and no snapshot in validate-only mode.

### Task 5: Owner-configured model continuity and shared pricing

**Files:**
- Create: `prototype/andrewbot/configuration.py`
- Modify: `prototype/andrewbot/main.py`
- Modify: `prototype/andrewbot/model.py`
- Modify: `prototype/andrewbot/gateway.py`
- Modify: `prototype/andrewbot/appserver.py`
- Modify: `prototype/andrewbot/controller.py`
- Create: `prototype/tests/test_configuration.py`
- Modify: `prototype/tests/test_model.py`
- Modify: `prototype/tests/test_gateway.py`
- Modify: `prototype/tests/test_appserver.py`

**Interfaces:**
- Produces immutable `ModelConfig(model_id: str, input_usd_per_million: float, output_usd_per_million: float)` and `load_model_config(environment: Mapping[str, str]) -> ModelConfig` in `configuration.py`.
- Reads `ANDREW_MODEL_ID`, `ANDREW_MODEL_INPUT_USD_PER_MILLION`, and `ANDREW_MODEL_OUTPUT_USD_PER_MILLION` from owner-managed server configuration, defaulting to current Qwen ID and its current rates.
- Adds one validated immutable model config passed into `OpenRouterModel`, `ModelGateway`, `Controller`, and `LettaAgent`; all components advertise, accept, request, and price the same model ID.
- Adds `AppServerClient.update_agent_model(agent_id: str, model_config: ModelConfig) -> None`, using the loopback Letta `PATCH /v1/agents/{agent_id}` route to update the already-persisted agent handle/settings when owner configuration changes. Persist the applied model ID only after Letta confirms the update.
- Rejects empty/malformed model IDs, non-finite/negative rates, and disagreement between Letta model handle and gateway model before any provider call. Does not add Telegram model switching or another provider credential path.

- [x] **Step 1: Write failing configuration and Letta update tests** `test_model_id_and_rates_are_shared_by_letta_and_gateway`, `test_owner_model_change_updates_existing_letta_agent_without_changing_memory`, `test_agent_model_update_uses_local_patch_endpoint`, `test_invalid_model_config_fails_before_provider_request`, and `test_configured_rates_drive_controller_estimates_and_gateway_reservations`.
- [x] **Step 2: Run `PYTHONPATH=prototype python3 -m unittest prototype.tests.test_configuration prototype.tests.test_model prototype.tests.test_gateway prototype.tests.test_appserver -v`** and confirm current hardcoded model behavior violates the new expectations.
- [x] **Step 3: Implement `ModelConfig` and `load_model_config`** with a bounded OpenRouter model-ID syntax, finite non-negative rates, current Qwen defaults, and fixed safe configuration errors.
- [x] **Step 4: Pass the same immutable config to `OpenRouterModel`, `ModelGateway`, `Controller`, and `LettaAgent`**; use its rates for both controller estimates and gateway reservations, and its model ID for Letta registration, gateway allowlisting, provider requests, and model listing. Update existing Letta agents through the local update endpoint when the persisted model ID differs.
- [x] **Step 5: Run `PYTHONPATH=prototype python3 -m unittest prototype.tests.test_configuration prototype.tests.test_model prototype.tests.test_gateway prototype.tests.test_appserver -v`** and confirm a model change only alters the configured endpoint/model selection, not memory state.

### Task 6: End-to-end continuity validation and phase documentation

**Files:**
- Create: `prototype/tests/test_memory_continuity.py`
- Modify: `PHASE0_INSTALL_PLAN.md`
- Modify: `PROJECT_DESCRIPTION.md`

**Interfaces:**
- Integrates the existing `Store`, `MemoryService`, `LettaAgent`, model config, and backup API using temporary databases and fake Letta/provider clients only.
- Documents the Phase 4 implementation state, how to run a memory backup/restore check, and the owner-managed model configuration keys without recording secrets.

- [x] **Step 1: Write an offline continuity test** `test_memory_retrieval_and_revision_history_survive_restart_and_model_change`; create records and a diary row, close/reopen the SQLite store, switch fake configured model, then assert the same revision IDs and retrieved records remain.
- [x] **Step 2: Run `PYTHONPATH=prototype python3 -m unittest prototype.tests.test_memory_continuity -v`** and confirm the integrated continuity path passes.
- [x] **Step 3: Update phase documentation** to record completed memory, retrieval, backup, and model continuity capabilities while stating the service and schedules remain disabled.
- [x] **Step 4: Run `PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -v`**. The full suite ran 69 tests: 67 passed and 2 live Letta tests were skipped because the service was unavailable. The Phase 2 budget test now checks approved allowance rollover; its wake test preserves the real `datetime` constructor while freezing the clock. All 50 Phase 4-focused tests pass.
- [x] **Step 5: Review the source activation gates and deployment script behavior**; confirm `ALL_PROJECT_PHASES_COMPLETE` remains false and neither the controller nor autonomous/scheduled sessions were started.

## Final Review

- Confirm every acceptance criterion in the Phase 4 spec is covered by a task and a named check.
- Confirm periodic local backups are invoked during normal controller operation and are not created during one-shot validation.
- Review secret handling, append-only writes, event/diary reference validation, FTS query handling, backup failure atomicity, and model/rate consistency.
- Confirm restore validation never overwrites a live DB in place and rejected edits never log raw content.
- Confirm no service, autonomous session, schedule, or external communication is enabled, and make no Git commit.

## Plan Self-Review

- Spec version history, date, author/source, reason, event references, audit entries, immutable owner records, retrieval, bounded context, restart continuity, model continuity, encrypted backups, and dormant activation gates map to Tasks 1–6.
- Each new module has a named API and targeted test names; shared model ID and rate config is declared once in `configuration.py`.
- FTS5 is available in the current Python SQLite build (`sqlite3` 3.45.1 with `ENABLE_FTS5`); the migration still fails closed if a future runtime lacks it.
- The backup credential is separate from the existing vault key; restores target a validated destination file, and periodic backups run only after normal service startup.
- No implementation, test, service start, session, or commit was performed while writing this plan.
