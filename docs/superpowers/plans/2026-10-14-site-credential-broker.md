# Site Credential Broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Andrew save credentials for owner-approved sites and use them for future logins without exposing secret values to the model or arbitrary code.

**Architecture:** Extend the trusted controller's encrypted vault with site-scoped account records and a narrow broker API. The broker validates exact approved origins and performs credential injection through a browser control channel unavailable to page scripts, the worker, and model. Registration remains unavailable on domains lacking owner review/approval.

**Tech Stack:** Python 3, SQLite, AES-GCM, systemd credentials, Chromium/Playwright over a broker-controlled browser channel.

**Spec:** `docs/superpowers/specs/2026-10-14-browser-code-history-design.md`

## Global Constraints

- Credential secret bytes never enter model input, Python/Node code, shell arguments, environment variables, diary, Telegram, Git, or ordinary logs.
- Records are encrypted at rest in the controller-owned vault and persist indefinitely until owner deletion/revocation.
- Broker actions are bound to an exact reviewed and owner-approved origin; redirects to another origin cancel credential use.
- No signup on an unapproved domain; no bypassing CAPTCHA, human identity checks, verification, rate limits, or anti-bot mechanisms.
- X and Gmail integrations remain deferred; this plan covers only site-scoped credentials for reviewed sites.
- Raw cookies/password-manager storage are not used as durable credential stores.

## Review Focus

- IDN, trailing-dot, port, scheme, and subdomain origin confusion: canonicalize origin and test all equivalent/ambiguous forms.
- Redirect or navigation after broker approval: cancel autofill when effective origin changes.
- Malicious page or worker attempts to read the secret: secret is never returned to model/code or injected into tool output; only the approved origin receives it for the intended form submission.
- Vault migration interruption: old records remain readable and migration is atomic/idempotent.
- Revoke during an active request: broker cancels future fills and reports any existing website session may require separate revocation.

---

### Task 1: Site policy and account schema

**Files:**
- Create: `prototype/andrewbot/site_policy.py`
- Modify: `prototype/andrewbot/vault.py`
- Create: `prototype/tests/test_site_policy.py`
- Modify: `prototype/tests/test_vault.py`

**Interfaces:**
- `canonical_origin(url: str) -> str`; returns lowercase `https://host[:port]`, rejects credentials in URL, IP literals, non-HTTPS, invalid IDNA, and non-default ports unless explicitly allowed.
- `SitePolicy.approve(origin: str, actions: frozenset[str], reviewed_at: datetime) -> None`; actions limited to `read`, `signup`, and `login`.
- `SitePolicy.permits(origin: str, action: str) -> bool`.
- `Vault.save_account(origin: str, username: str, secret: str, account_id: str | None = None) -> str`; returns opaque account ID.
- `Vault.account_metadata() -> list[dict]`, `Vault.get_account_secret(account_id: str, origin: str) -> str`, `Vault.revoke_account(account_id: str) -> bool`, `Vault.delete_account(account_id: str) -> bool`.

- [ ] **Step 1: Add failing policy tests** for canonical origins, IDNA, query/fragment removal, userinfo rejection, ports, scheme downgrade, subdomain mismatch, and missing action grant.
- [ ] **Step 2: Run** `pytest prototype/tests/test_site_policy.py -q` and confirm expected failures.
- [ ] **Step 3: Implement `SitePolicy`** with exact-origin matching; no wildcard subdomains.
- [ ] **Step 4: Add failing vault migration tests** from the current `credentials(name, nonce, ciphertext, updated_at)` schema and interrupted migration simulation.
- [ ] **Step 5: Implement atomic vault migration** adding `site_accounts` with account ID, canonical origin, username ciphertext, secret ciphertext, metadata timestamps, and revocation state; use unique nonces and AEAD associated data containing record ID, field name, and schema version.
- [ ] **Step 6: Verify** existing Telegram/OpenRouter/X records remain readable after migration and account secrets are not present in SQLite plaintext.

### Task 2: Origin-bound credential broker

**Files:**
- Create: `prototype/andrewbot/credential_broker.py`
- Create: `prototype/tests/test_credential_broker.py`
- Modify: `prototype/andrewbot/controller.py`
- Modify: `prototype/andrewbot/main.py`

**Interfaces:**
- `CredentialBroker.login(account_id: str, origin: str, browser: BrowserControl) -> BrokerResult`; checks policy and active account, then directs the browser control channel to fill and submit without returning the secret.
- `CredentialBroker.begin_signup(origin: str, username: str, browser: BrowserControl) -> str`; checks signup grant, creates a unique password internally, stores it through Vault, and fills it through the trusted browser channel. Returns only the opaque account ID.
- `BrokerResult` contains only `status`, `account_id`, `origin`, and fixed safe error category.

- [ ] **Step 1: Add failing broker tests** for approved login, unapproved origin, origin mismatch, redirect, revoked/missing record, failed browser channel, and successful broker-generated signup credential persistence.
- [ ] **Step 2: Run** `pytest prototype/tests/test_credential_broker.py -q` to confirm missing behavior.
- [ ] **Step 3: Define a trusted `BrowserControl` protocol** implemented outside arbitrary worker code; only the broker can request credential-field fill and submit, and the interface never returns field values or DOM secrets.
- [ ] **Step 4: Implement broker operations**; generate signup passwords inside the trusted broker using a cryptographically secure generator, save before submitting the form, and ensure exceptions/logs/results contain no secret material. Never accept password values from the model or worker.
- [ ] **Step 5: Verify** malformed or unauthorized calls do not decrypt a secret and active origin changes cancel the fill.

### Task 3: Controller tools and owner management

**Files:**
- Modify: `prototype/andrewbot/appserver.py`
- Modify: `prototype/andrewbot/controller.py`
- Modify: `prototype/andrewbot/telegram.py`
- Modify: `prototype/andrewbot/main.py`
- Create: `prototype/tests/test_account_tools.py`
- Modify: `prototype/tests/test_telegram.py`

- [ ] **Step 1: Add failing tool tests** for `accounts.list`, `accounts.login`, `accounts.save_signup`, and owner-only approve/revoke/delete operations.
- [ ] **Step 2: Run** focused tests and verify tool dispatch and owner controls fail.
- [ ] **Step 3: Implement model tools** that return metadata and fixed outcomes only; require exact origin and action; never return secret values.
- [ ] **Step 4: Implement owner controls** to list account metadata, approve origin/action with review date, revoke, and delete records; emit audit events containing IDs/operation/outcome only.
- [ ] **Step 5: Verify** unapproved registration, CAPTCHA, identity verification, and anti-bot prompts stop without retry or workaround.

### Task 4: Signup and login browser integration

**Files:**
- Modify: `prototype/andrewbot/browser.py`
- Modify: `prototype/andrewbot/credential_broker.py`
- Create: `prototype/tests/test_account_browser_integration.py`

- [ ] **Step 1: Add a fake-browser integration test** proving the secret is available only to the trusted browser control channel and intended approved-origin form submission, never to tool result, worker stdout/stderr, or model transcript.
- [ ] **Step 2: Design the fill channel** to remain outside the arbitrary-code worker. If the current runner architecture cannot guarantee that, stop and revise the architecture before implementing account use.
- [ ] **Step 3: Implement reviewed-domain signup/login** with exact origin checks before every navigation, field fill, and submit; save credentials before reporting signup success.
- [ ] **Step 4: Verify** redirect, unexpected form, unsupported challenge, policy change, and broker crash all fail closed and do not report success.
- [ ] **Step 5: Stop for owner review** before enabling signup/login on any real site.
