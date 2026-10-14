# Encrypted Browser History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store sanitized browser visit records indefinitely and provide owner-controlled search and deletion.

**Architecture:** The browser adapter emits a visit event to the trusted controller after each successful navigation. A dedicated SQLite store encrypts records with a history-specific key loaded through systemd credentials. Owner controls operate on metadata and encrypted rows, independent of agent memory and diary.

**Tech Stack:** Python 3, SQLite, AES-GCM via `cryptography`, systemd encrypted credentials, existing Telegram owner controls.

**Spec:** `docs/superpowers/specs/2026-10-14-browser-code-history-design.md`

## Global Constraints

- Retain history indefinitely until the owner deletes it; never auto-expire or silently evict old records.
- Store timestamp, title, sanitized URL only; strip query and fragment and do not store bodies, forms, passwords, cookies, or authorization headers.
- History remains outside autobiographical memory, diary, objective audit log, and worker workspace.
- Encrypt at rest with a key separate from the credential vault key.
- If storage is unavailable or full, stop navigation/recording and alert the owner; preserve older records.
- Owner can search, delete one visit, or delete all history.

## Review Focus

- URLs containing secrets in query or fragment: sanitize before persistence and assert raw values are absent from database/WAL.
- Database full or read-only: fail the navigation closed and do not delete old visits.
- SQLite WAL, backup, and deletion: document and test full-delete handling across database sidecars and project backups.
- Malformed titles or large text: normalize and cap fields without storing page body.
- Unauthorized Telegram user: deny search/delete and leave state unchanged.

---

### Task 1: Encrypted history store

**Files:**
- Create: `prototype/andrewbot/browser_history.py`
- Create: `prototype/tests/test_browser_history.py`
- Modify: `prototype/andrewbot/main.py`

**Interfaces:**
- `VisitHistory(directory: Path, key: bytes, max_bytes: int).record(url: str, title: str, visited_at: datetime) -> int`
- `VisitHistory.search(query: str, limit: int = 20) -> list[dict]`; returns only `id`, `visited_at`, `title`, `url`.
- `VisitHistory.delete(visit_id: int) -> bool`; `delete_all() -> int`.
- `sanitize_visit(url: str, title: str) -> tuple[str, str]`; strips query/fragment, rejects non-HTTP(S), caps title at 512 characters and URL at 2048 characters.

- [ ] **Step 1: Write failing tests** for encrypted persistence, URL sanitization, field limits, search, single deletion, delete-all, restart persistence, invalid scheme, and encryption authentication failure.
- [ ] **Step 2: Run** `pytest prototype/tests/test_browser_history.py -q` to confirm failures.
- [ ] **Step 3: Implement storage** with AES-GCM per row, random 96-bit nonce, visit ID as associated data, SQLite `0600`, database directory `0700`, WAL disabled or safely included in encrypted-record design, and explicit byte ceiling.
- [ ] **Step 4: Test** raw URL queries/fragments, title, and URL do not appear in DB bytes, WAL, or logs; verify search decrypts only through `VisitHistory`.
- [ ] **Step 5: Implement capacity behavior** that raises `HistoryCapacityError` without deleting rows; verify count and prior rows remain unchanged after capacity failure.

### Task 2: Controller and browser integration

**Files:**
- Modify: `prototype/andrewbot/browser.py`
- Modify: `prototype/andrewbot/tool_runtime.py`
- Modify: `prototype/andrewbot/controller.py`
- Modify: `prototype/andrewbot/main.py`
- Test: `prototype/tests/test_browser.py`
- Test: `prototype/tests/test_controller.py`

**Interfaces:**
- Browser navigation callback: `on_visit(url: str, title: str, visited_at: datetime) -> int`.
- Controller accepts `visit_history=None`; successful browser navigation records before returning page content. If record fails, navigation result is discarded and a safe storage error returned.

- [ ] **Step 1: Add failing integration tests** for successful visit recording, failed record preventing content return, and no recording of failed navigation.
- [ ] **Step 2: Run** the focused browser/controller tests and confirm expected failures.
- [ ] **Step 3: Wire history callback** from browser tool through trusted controller, never passing the history store into the worker.
- [ ] **Step 4: Verify** browser sees only its own response; it cannot read history DB or encryption key.

### Task 3: Owner search and deletion

**Files:**
- Modify: `prototype/andrewbot/controller.py`
- Modify: `prototype/andrewbot/telegram.py`
- Modify: `prototype/andrewbot/main.py`
- Modify: `prototype/andrewbot/appserver.py`
- Test: `prototype/tests/test_controller.py`
- Test: `prototype/tests/test_telegram.py`
- Test: `prototype/tests/test_appserver.py`

- [ ] **Step 1: Add failing owner-command tests** for `/history [query]`, `/history delete <id>`, and `/history delete all`, including non-owner rejection and confirmation text.
- [ ] **Step 2: Run focused tests** and confirm owner controls are not implemented.
- [ ] **Step 3: Implement commands and model tools** with max 20 results, query length 256 characters, and metadata-only audit records. Full deletion requires explicit Telegram command phrase `DELETE ALL HISTORY` as a second confirmation.
- [ ] **Step 4: Verify** unauthorized requests do not call store methods and audit output includes no URL/title/query text.

### Task 4: Persistence key, backups, and capacity operations

**Files:**
- Modify: `systemd/andrew-control.service`
- Modify: `systemd/andrew-control.service.d/credentials.conf`
- Create: `scripts/andrew-history-maintenance`
- Create: `docs/browser-history-operations.md`
- Test: `prototype/tests/test_browser_history.py`

- [ ] **Step 1: Define separate history key loading** using `LoadCredentialEncrypted=andrew-history-key`; refuse startup if the key is absent or invalid.
- [ ] **Step 2: Add maintenance commands** for encrypted backup export, restore validation, capacity report, and full deletion including SQLite sidecars and backup copies per documented retention/deletion path.
- [ ] **Step 3: Document** owner recovery, deletion scope, backup behavior, key loss behavior, and full-capacity response.
- [ ] **Step 4: Verify** restart persistence, restore, and deletion on disposable data only; no production history deletion during verification.
