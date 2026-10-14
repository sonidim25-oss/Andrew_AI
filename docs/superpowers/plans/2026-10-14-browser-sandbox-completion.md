# Browser and sandbox completion

**Owner authorization:** Complete browser/sandbox integration and prove network isolation, worker limits, browser operations and site-account use. The owner explicitly authorizes full browser control, logins and signups without per-site or per-action approval prompts. Preserve sandbox and spending boundaries. No real site credentials have been supplied for verification; use controlled fixtures for account proof and make the general browser available afterward.

**Base:** `cb7e3a69cdc3e1dc2c98bc940a2cc8b45d1d35d9`.
**Execution:** Inline under executing-plans; test each contract before implementation. Do not interrupt the running daily controller until deployment is concrete and locally proved. This plan updates the earlier browser/runtime and credential-broker plans where the owner changed permission scope or the original architecture could not protect credentials.

## Architecture and constraints

- Rootless disposable code workers: no IP network, dropped capabilities, read-only root, no new privileges, dedicated projects mount and egress socket only. Fixed ceilings: two CPUs, 2 GiB RAM, 256 tasks, 120 seconds and 10 GiB persistent projects filesystem. Capture output with a host-side bound too; serialize workers.
- Separate trusted browser identity/container: no arbitrary Python/shell/evaluate interface, no projects or credential-store mount. Private controller-only control socket; ephemeral profile. Same outer network/resource limits and one browser session. Preserve the previously owner-approved Chromium `--no-sandbox` exception inside that outer boundary.
- Broad browser permission follows the owner's blanket grant. Exact-origin checks still bind each stored credential and each credential submission. No wildcard credential reuse across domains. No per-site approval UI.
- Account passwords are generated/stored by the encrypted vault and sent only over the private browser channel. Substitute credentials into the intended exact-origin POST after page script processing; never put actual passwords in page DOM fields, model output, code-worker inputs, argv, environment, screenshots or logs. Redact known credentials from all browser results. No persistent browser cookie export.
- Public network access only through the Unix proxy: DNS validation, address pinning, explicit special-purpose/host-network exclusions, and an independently enforced Andrew-only UID firewall table. Never change unrelated host firewall tables or services.
- Browser actions include navigation, tabs, bounded DOM snapshots, click/fill/select/keys/scroll, screenshots and file transfers through opaque artifact IDs or validated workspace paths. Account signup/login uses the same trusted browser control layer.
- Stop unsupported human verification/challenges without bypassing them; record a blocked outcome instead of inventing success.
- Preserve owner pause/sleep, model gateway budgets and the existing daily lifecycle. Expose browser/code tools only when an administrator-owned proof record matches installed code, image and isolation policy. Do not label local/component tests as VPS acceptance.

## Tasks

### Task 1: Worker and proxy boundaries

1. Reproduce rootless launch with the existing image and inspect actual kernel limits/network/mounts.
2. Add failing tests for host-side bounded output, one-worker concurrency, strict request framing, proxy connection limits, special-purpose/host address blocks and CONNECT port restrictions.
3. Implement those boundaries; preserve pinned images and fixed production ceilings.
4. Run focused and complete unit suites. Record which checks are real rootless runs.

### Task 2: Trusted browser control

1. Add failing browser protocol and operation tests, including hostile page content and malformed commands.
2. Implement the private browser service/client and fixed container entrypoint, separate from arbitrary code workers.
3. Cover navigation, DOM interaction, tabs, screenshots/file transfer bounds and single-session cleanup. Return bounded untrusted data only.
4. Exercise a real Chromium instance against a controlled fixture with no production credentials or persistent profile.

### Task 3: Account broker and broad owner policy

1. Add failing origin-bound signup/login, encrypted persistence, revocation/deletion and redaction tests.
2. Implement the broker and blanket public-site action policy without per-site approval prompts; reuse encrypted account records.
3. Prove password substitution occurs outside the page DOM and code worker; block origin changes, unexpected forms, challenge flows and revoked accounts.
4. Exercise signup, logout, login after restart and revocation against a controlled fixture. Do not create random third-party accounts as test artifacts.

### Task 4: Controller integration and proof gate

1. Add failing tests for tool schemas/permissions, owner controls and missing/stale/mismatched readiness proofs.
2. Wire the browser/broker/history into the controller and pinned Letta protocol. Preserve strict mode and exact tool allowlists.
3. Gate browser/code enablement on proof plus owner configuration; keep budget and lifecycle validation in front of every call.
4. Run the real isolated Letta harness with browser callbacks before proposing VPS deployment.

### Task 5: VPS provisioning and acceptance

1. Prepare one administrator command with private preflight snapshots, scoped rollback, dedicated identities, pinned image installation, workspace filesystem cap and suitable rootless cgroup delegation.
2. Validate nftables syntax and install only the Andrew UID table. Preserve existing tables and capture before/after hashes privately.
3. In disposable workers prove direct public/private/DNS/host networking is absent; approved proxy HTTPS works; rebinding/private redirects and unavailable-proxy paths fail closed.
4. Prove actual cgroup CPU, memory, PID and time enforcement and dedicated filesystem capacity; ensure cleanup and existing service health.
5. Prove real browser operations and account fixtures without secrets in outputs, process arguments/environment, history, logs or model transcript.
6. Write a root-owned proof record only on success, then install/enable the reviewed browser capabilities without resetting spending or lifecycle records.

### Task 6: Final review and status

1. One fresh whole-branch review of code, proofs and failure paths; fix important findings with regressions.
2. Update FEATURE_STATUS with distinct source/local/VPS/live/autonomous results and remaining external dependencies.
3. Commit reviewable changes. Preserve a concrete administrator handoff if OS sudo requires owner execution; do not request authorization again for this already-approved work.

## Review focus

Cross-container credential/session separation; browser response/DOM secret leakage; origin/form substitution and redirects; code-worker access to the browser channel; fake/stale proof records; actual cgroup enforcement versus configured flags; workspace capacity versus individual file size; runner output/concurrency exhaustion; DNS rebinding/private/host-public-address proxy bypasses; unrelated firewall/service preservation; pause/sleep/budget gates after browser tools become available.
