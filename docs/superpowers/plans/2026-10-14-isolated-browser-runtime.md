# Isolated Browser and Code Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide Andrew with an isolated public browser and Python/Node.js execution over proxy-only public HTTPS.

**Architecture:** The controller sends typed requests over a restricted Unix socket to a rootless Podman runner. The disposable worker has no IP networking and receives only a dedicated workspace and proxy socket. A separate egress process performs DNS resolution, destination validation, and HTTP(S) forwarding.

**Tech Stack:** Python 3, Ubuntu 24.04 systemd, rootless Podman, Chromium/Playwright, Unix domain sockets, nftables scoped to `andrew-egress`.

**Spec:** `docs/superpowers/specs/2026-10-14-browser-code-history-design.md`

## Global Constraints

- Worker network mode is `none`; no direct DNS or IP route.
- Worker mounts only `/srv/andrew/workspace/projects` and the egress Unix socket. It must never mount the parent workspace containing diary, memory, or downloads.
- Worker ceilings: 2 CPU cores, 2 GiB memory, 256 processes, 120 seconds per shell request, 10 GiB workspace.
- Browser concurrency is one session with an initial 2 GiB memory ceiling.
- Owner explicitly approved Chromium `--no-sandbox` inside the outer rootless, networkless, capability-free container because nested user namespaces are denied in this environment; preserve all outer isolation limits.
- Allow only public HTTP/HTTPS through the proxy; reject private, reserved, loopback, link-local, metadata, multicast, and non-global destinations.
- Resolve and pin addresses at the proxy; repeat destination checks after redirects.
- Do not modify existing Docker, UFW, Tailscale, website, Telegram bot, or firewall tables.
- Keep browser/code tools disabled until isolation acceptance checks pass.
- Scheduled autonomous sessions remain disabled until a manually observed 5–10 minute session succeeds.

## Review Focus

- DNS rebinding or redirect to private address: proxy rejects before connection; test both initial resolution and redirect target.
- Proxy outage or malformed socket request: runner fails closed with bounded safe error; test no direct-network fallback.
- Resource exhaustion: runner kills only its worker at CPU, RAM, PID, wall-time, and workspace limits; test each limit in disposable containers.
- Web content prompt injection: browser returns content as untrusted data and cannot alter policy; test malicious page text does not grant tools or permissions.
- Host filesystem exposure: worker cannot read host services, vault, browser history, diary, memory, Docker socket, or unrelated files; verify mount list and negative access checks.

---

### Task 1: Runner request protocol and controller adapter

**Files:**
- Create: `prototype/andrewbot/runner_client.py`
- Create: `prototype/andrewbot/tool_runtime.py`
- Modify: `prototype/andrewbot/controller.py`
- Modify: `prototype/andrewbot/appserver.py`
- Test: `prototype/tests/test_runner_client.py`
- Test: `prototype/tests/test_tool_runtime.py`
- Test: `prototype/tests/test_controller.py`
- Test: `prototype/tests/test_appserver.py`

**Interfaces:**
- `RunnerClient(socket_path: str, timeout_seconds: int = 120).run(request: dict) -> dict`; request types are `python`, `node`, `shell`, and `browser_open` with schema-validated arguments. Response contains only `status`, `stdout`, `stderr`, `artifacts`, and `safe_error`.
- `ToolRuntime(runner: RunnerClient, policy: ToolPolicy).execute(name: str, arguments: dict) -> str`; rejects disabled tools and malformed arguments before socket access.
- Controller accepts an optional `tool_runtime=None`; absent runtime means all runtime actions reject as unavailable.

- [ ] **Step 1: Add failing protocol tests** for valid bounded JSON frames, oversized frames, unknown request types, malformed JSON, socket timeout, and socket unavailable.
- [ ] **Step 2: Run the focused tests** with `pytest prototype/tests/test_runner_client.py -q`; confirm failures are limited to missing implementation.
- [ ] **Step 3: Implement `RunnerClient`** with a 64 KiB request-frame ceiling, 16 KiB response-frame ceiling, strict UTF-8 JSON, 120-second default timeout, and no exception text copied into returned `safe_error`.
- [ ] **Step 4: Test protocol behavior** with a local fake Unix socket; assert timeout and malformed responses fail closed.
- [ ] **Step 5: Add failing controller/tool schema tests** for `python.run`, `node.run`, `shell.run`, and `browser.open`; require strict argument keys, string code up to 64 KiB, and absolute `https://` URL for browser navigation.
- [ ] **Step 6: Implement `ToolRuntime` and controller dispatch**; add external tool schemas to `AppServerClient.CONTROLLER_TOOLS` and route only through `Controller.execute_tool`.
- [ ] **Step 7: Verify** controller and App Server focused tests; check that no Letta native `Bash`, `Browser`, or filesystem tool is enabled.

### Task 2: Egress proxy and destination policy

**Files:**
- Create: `prototype/andrewbot/egress_proxy.py`
- Create: `prototype/tests/test_egress_proxy.py`
- Create: `systemd/andrew-egress.service`
- Create: `systemd/andrew-egress.socket`
- Modify: `firewall/README.md`

**Interfaces:**
- `DestinationPolicy.resolve_and_validate(host: str, port: int) -> tuple[str, ...]`; returns only validated public addresses, raises `ProxyPolicyError` otherwise.
- `EgressProxy.handle_request(reader, writer) -> None`; accepts HTTP proxy requests on a Unix socket and HTTP CONNECT only to port 443 (port 80 for plain HTTP); never logs request bodies, auth headers, cookies, or query strings.

- [ ] **Step 1: Add failing policy tests** for public IPv4/IPv6, all blocked address classes in the spec, mixed public/private DNS answers, IP literal, invalid port, DNS failure, and changing DNS answers.
- [ ] **Step 2: Run** `pytest prototype/tests/test_egress_proxy.py -q`; verify the cases fail before implementation.
- [ ] **Step 3: Implement `DestinationPolicy`** using standard-library IP classification plus explicit blocked ranges from `PHASE1_IMPLEMENTATION_PLAN.md`; reject the entire DNS answer set if any address is blocked and pin one validated address per connection.
- [ ] **Step 4: Add failing proxy tests** for HTTP request forwarding, CONNECT handling, redirect revalidation, private redirect rejection, request size ceilings, and no sensitive content in captured logs.
- [ ] **Step 5: Implement `EgressProxy`** with bounded headers/body/timeouts and no TCP listener; destination checks occur before connect and on every redirect.
- [ ] **Step 6: Define socket-activated systemd units** running only as `andrew-egress`, with `NoNewPrivileges`, filesystem protection, and access to resolver plus public 80/443 only.
- [ ] **Step 7: Update the firewall README** with an Andrew-only nftables review diff and rollback instructions; do not apply firewall rules in this task.
- [ ] **Step 8: Verify** unit tests and static unit review. Host firewall changes require a separate reviewed, root-operated deployment step.

### Task 3: Rootless worker image and bounded runner

**Files:**
- Create: `runtime/Containerfile`
- Create: `runtime/worker_entry.py`
- Create: `prototype/andrewbot/runner_service.py`
- Create: `prototype/tests/test_runner_service.py`
- Create: `prototype/tests/test_worker_entry.py`
- Create: `systemd/andrew-runner.socket`
- Create: `systemd/andrew-runner.service`
- Modify: `scripts/validate-and-start-controller.sh`

**Interfaces:**
- `WorkerConfig` is immutable and pins image digest, workspace mount, proxy socket, CPU, memory, PID, and wall-time ceilings.
- `RunnerService.run(request: dict) -> dict` creates one disposable Podman worker with `--network=none`, drops capabilities, uses read-only root filesystem and bounded `/tmp`, captures bounded outputs, and removes the worker in `finally`.

- [ ] **Step 1: Add failing runner tests** for fixed Podman argument construction, only two mounts, no secret mounts, request allowlist, output truncation, timeout cleanup, and worker removal after errors.
- [ ] **Step 2: Run** `pytest prototype/tests/test_runner_service.py prototype/tests/test_worker_entry.py -q` and confirm missing behavior.
- [ ] **Step 3: Build a pinned minimal image** containing Python, Node.js, Chromium, Playwright, and required dependencies; pin package versions and image digest in the build record.
- [ ] **Step 4: Implement worker entry** to run one request, cap stdout/stderr at 16 KiB each, and return artifacts only under `/workspace`.
- [ ] **Step 5: Implement runner service** using fixed Podman args: `--network=none`, rootless `--userns=keep-id:uid=1000,gid=1000`, unprivileged UID 1000, read-only image root, dropped capabilities, no-new-privileges, 2 CPU, 2 GiB RAM, 256 PIDs, 120-second timeout, 10 GiB quota on the isolated projects filesystem, and one proxy socket mount. Mount only `/srv/andrew/workspace/projects`, never the parent containing diary, memory, or downloads.
- [ ] **Step 6: Add isolated integration proof scripts** that inspect `podman inspect`, mount list, routes, DNS, host listeners, metadata, private destinations, public HTTPS by proxy, and resource ceilings using disposable containers only.
- [ ] **Step 7: Keep startup validation fail closed** when runner/proxy isolation proof status is absent or stale; do not automatically start either runtime service.
- [ ] **Step 8: Verify** unit and integration checks on a disposable worker, recording proof artifacts without secrets. Do not alter host firewall or enable services as part of test execution.

### Task 4: Browser and code tool behavior

**Files:**
- Create: `prototype/andrewbot/browser.py`
- Create: `prototype/tests/test_browser.py`
- Modify: `prototype/andrewbot/tool_runtime.py`
- Modify: `prototype/andrewbot/appserver.py`

**Interfaces:**
- `Browser.open(url: str) -> BrowserResult`; accepts only HTTP/HTTPS public URL and returns title, sanitized final URL, and visible text capped at 16 KiB. It does not return cookies, storage state, form values, or page source.
- Tool names: `python.run(code)`, `node.run(code)`, `shell.run(command)`, `browser.open(url)`; each tool returns bounded output and untrusted-content annotation.

- [ ] **Step 1: Add failing browser tests** for HTTPS navigation, disallowed schemes, redirect policy, output truncation, script-generated malicious instructions, and omitted cookies/forms.
- [ ] **Step 2: Run** `pytest prototype/tests/test_browser.py -q` and verify the missing implementation.
- [ ] **Step 3: Implement Browser with Playwright** in the ephemeral worker, one navigation at a time, 30-second page load ceiling, 16 KiB text ceiling, and no persisted profile.
- [ ] **Step 4: Ensure returned page text is clearly untrusted** and controller instructions cannot be modified by page content.
- [ ] **Step 5: Verify** all browser/code controller calls go through the runner and no secret or host path is present in worker inputs.

### Task 5: Controlled activation gate

**Files:**
- Modify: `scripts/validate-and-start-controller.sh`
- Modify: `systemd/andrew-control.service`
- Modify: `PHASE1_IMPLEMENTATION_PLAN.md`

- [ ] **Step 1: Add a validation report** recording worker/proxy proof versions, timestamp, image digest, mount policy hash, and firewall policy hash; no credentials or browsing data.
- [ ] **Step 2: Add failing validation tests** for missing, stale, or mismatched proof records and ensure browser/code tools stay disabled.
- [ ] **Step 3: Implement the one-shot check**; validation reports only safe check names and status categories.
- [ ] **Step 4: Verify** `--validate-only` never starts a long-running controller or runtime service.
- [ ] **Step 5: Stop for owner review** before applying Andrew-scoped firewall policy or enabling services.
