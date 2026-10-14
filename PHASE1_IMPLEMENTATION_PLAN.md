# Phase 1 Implementation Plan

> **Status (updated 2026-10-14):** Workspace prototype implementation is in progress. The controller and tool runtime are not activated by this repository. No production credentials, host inventory, or live firewall snapshot belong in this public plan. See README.md for verified local checks and remaining deployment gates.

**Goal:** Establish a persistent Letta-based Andrew runtime with trusted control services, an encrypted credential vault, and an isolated rootless tool worker, without changing the existing website or Telegram bots.

**Architecture:** The target architecture uses a trusted controller for Telegram, the credential vault, model gateway, budget ledger, audit log and lifecycle APIs. Letta is installed as the persistent agent layer, without shell tools. A separate rootless Podman worker remains disabled until its network and filesystem isolation can be demonstrated. The current prototype controller uses SQLite conversation memory and direct OpenRouter requests; its Letta agent/gateway integration remains unfinished.

**Tech stack:** Ubuntu 24.04 systemd, rootless Podman, Letta App Server, SQLite with per-record AEAD encryption, systemd encrypted credentials/TPM2 where supported, a controller-owned OpenAI-compatible model gateway, Unix domain sockets, and nftables rules scoped to the new service identities.

**Spec:** `PROJECT_DESCRIPTION.md` and `ARCHITECTURE_REVIEW.md`.

## Proposed Linux identities

Create these system accounts with locked passwords, no interactive shell, no SSH access, no `sudo` membership, and no membership in the existing `docker` group:

| Account | Purpose | Permissions |
|---|---|---|
| `andrew-control` | Trusted controller, Telegram handler, model gateway, credential broker, lifecycle and audit state | Can read the encrypted vault key through a systemd credential and read/write only its own persistent state. Can call the runner over one restricted Unix socket. |
| `andrew-letta` | Letta App Server and persistent agent database | Owns Letta data only. No Telegram token, OpenRouter key, vault key, Docker socket, arbitrary host shell, or outbound network route. |
| `andrew-runtime` | Rootless Podman runner and Andrew's writable workspace | Can start only the fixed rootless worker configuration. No sudo, Docker group, controller data, vault access, or access to existing service paths. |
| `andrew-egress` | HTTP(S) egress proxy process | Can resolve DNS and connect to permitted public destinations through narrowly scoped host egress rules. It receives no credentials. |

Allocate non-overlapping subordinate UID/GID ranges in `/etc/subuid` and `/etc/subgid` for `andrew-letta` and `andrew-runtime`, each at least 65,536 IDs. Do not add either account to the rootful Docker group. Do not change existing host accounts or their access.

## Proposed paths and ownership

| Path | Owner and mode | Contents |
|---|---|---|
| `/etc/andrew/` | `root:root`, `0755`; configuration files `0644` | Non-secret service configuration and policy. No tokens or passwords. |
| `/etc/systemd/system/andrew-*.service` and `*.socket` | `root:root`, `0644` | Service and socket units; immutable to all Andrew service accounts. |
| `/etc/credstore.encrypted/andrew-vault-key` | `root:root`, `0600` | TPM2-sealed systemd credential where supported; otherwise host-key encrypted systemd credential with that limitation documented. |
| `/var/lib/andrew/control/` | `andrew-control:andrew-control`, `0700` | Controller state, lifecycle database, wake requests, Telegram queue, structured audit events and non-secret configuration state. |
| `/var/lib/andrew/vault/credentials.sqlite3` | `andrew-control:andrew-control`, `0600` | Account metadata and individually AEAD-encrypted secret fields. Never mounted into Letta or the tool worker. |
| `/var/lib/andrew/letta/` | `andrew-letta:andrew-letta`, `0700` | Letta state/database and only the volumes required by its pinned release. Use PostgreSQL with `pgvector` only if the chosen release/backend requires it. |
| `/var/lib/andrew/runtime/` | `andrew-runtime:andrew-runtime`, `0700` | Rootless Podman image and container state. No controller or vault files. |
| `/srv/andrew/workspace/{projects,diary,memory,downloads}/` | `andrew-runtime:andrew-runtime`, `0700` | Persistent agent workspace. These are the only writable host directories mounted into the tool worker. |
| `/srv/andrew/browser-profile/` | `andrew-runtime:andrew-runtime`, `0700` | Reserved for the future browser identity phase; do not use for account actions in Phase 1. |
| `/run/andrew/` | root-owned directory, `0755`; individual sockets restricted | Volatile runner and egress sockets. No general host `/run` mount. |

The workspace is capped at 10 GiB. Before provisioning, check whether ext4 project quotas are already supported and enabled. If not, use a separate 10 GiB loopback filesystem image for `/srv/andrew/workspace`; do not remount or change quota settings on the production root filesystem. The filesystem image and mount unit are new, separately removable resources.

## Proposed services and listeners

Create only these new units:

| Unit | Run as | Function and exposure |
|---|---|---|
| `andrew-control.service` | `andrew-control` | Trusted controller, owner-only Telegram commands (`/status`, `/wake`, `/sleep`, `/pause`, `/activity`, `/diary`, `/budget`, `/help`), encrypted vault access, local model gateway, budget enforcement, log redaction, backup coordination and kill switch. Uses `LoadCredentialEncrypted=` for the vault key. |
| `andrew-letta.service` | `andrew-letta` | Pinned Letta App Server with persistent state, configured only with controller-owned external tools. Bind its API to loopback only (proposed `127.0.0.1:8283`) with authentication. No general shell tool. |
| `andrew-runner.socket` / `andrew-runner.service` | socket owned for `andrew-control`; service runs as `andrew-runtime` | Controller submits a validated tool request; runner creates a short-lived rootless Podman worker, captures structured output and destroys the worker. The socket is not mounted into the worker. |
| `andrew-egress-agent.socket` | `andrew-runtime`, mode `0600` | The only network-like capability mounted into the worker: a Unix socket to a policy-enforcing HTTP(S) proxy. |
| `andrew-egress-control.socket` | `andrew-control`, mode `0600` | Lets the trusted controller call OpenRouter and Telegram through a separate proxy policy. |
| `andrew-egress.service` | `andrew-egress` | Socket-activated proxy. No TCP/UDP listener on the host. Resolves destinations itself, pins each resolved address, rejects private or reserved destinations, and does not log bodies, authorization headers, cookies, or query secrets. |

The controller-to-Letta model endpoint is private to the local control plane. Letta uses an internal opaque controller token; the OpenRouter key remains in the vault and is used by the controller's model gateway. The gateway checks token ceilings and budget before sending, reconciles provider usage afterwards, and reserves the $1 wrap-up allowance. If the pinned Letta release cannot use the gateway with tool calls and streaming, stop and revise the plan rather than placing the OpenRouter key in the agent worker.

Do not install or enable the daily fallback timer in Phase 1. Phase 2 implements Andrew-directed lifecycle scheduling but keeps live schedules disabled until all project phases are complete. No new TCP/UDP host port is expected apart from the proposed loopback-only Letta API; the Telegram bot uses outbound long polling through the proxy. Never bind a service to `0.0.0.0` or `[::]`.

## Rootless worker configuration

Install the Ubuntu 24.04 rootless runtime after approval. The current apt cache lists Podman 4.9.3, crun 1.14.1, uidmap 4.13, fuse-overlayfs 1.13 and slirp4netns 1.2.1 as candidates; verify current package versions before installing. The worker uses `--network=none`, a non-writable image, a non-root container UID where Chromium permits it, all capabilities dropped, `no-new-privileges`, a read-only root filesystem, bounded `/tmp`, and the default seccomp/AppArmor profiles. Do not use `--privileged`, host networking, host PID/IPC namespaces, devices, or any Docker/Podman socket.

Mount only:

- `/srv/andrew/workspace` at `/workspace` as read/write.
- The single worker egress socket at `/run/andrew-egress.sock`.

Do not mount host home directories, `/etc`, `/root`, `/var/run`, SSH paths, website or bot source/data, existing Docker volumes, or controller/Letta state. Use these initial ceilings: 2 CPU cores, 2 GiB memory, 256 processes, 120 seconds per shell request, and 10 GiB workspace. A browser session is single-concurrency and initially limited to 2 GiB. The runner, not the model, enforces these limits.

The container's network namespace has no IP interface, default route, DNS path or access to host loopback. The runner starts a fixed, minimal HTTP proxy bridge in the worker that forwards requests to the one mounted host Unix socket; it does not accept arbitrary destinations or provide a general host RPC. The agent can change its proxy environment or bridge, but cannot create a network route; direct connections therefore fail. The egress proxy accepts only HTTP/HTTPS, limits CONNECT to approved HTTP(S) ports, resolves names itself, rejects IP literals that are not public global-unicast addresses, rejects any answer in a blocked range, pins the selected address for that connection and repeats validation after each redirect. It blocks:

- IPv4 loopback, RFC1918, link-local/metadata, carrier-grade NAT, multicast and reserved ranges.
- IPv6 loopback, unique-local, link-local, multicast and other non-global-unicast ranges.
- Any host-specific LAN, container, VPN, and special-purpose ranges discovered during private preflight.
- Hostnames resolving to any blocked range, DNS rebinding results, and non-HTTP(S) protocols.

## Proposed firewall changes

Keep root-only host inventories and firewall snapshots outside this repository. Inspect the current firewall state privately before proposing any change. Do not modify existing firewall tables or apply an Andrew firewall policy without an owner-reviewed diff and rollback plan. Any later proposal must be a separate `inet andrew_agent` table scoped to the numeric UID of `andrew-egress`, and must not flush or edit existing UFW, Docker, Tailscale, or nftables tables.

Within that table, permit the proxy identity to use the local resolver on `127.0.0.53:53` (UDP/TCP) and to make TCP connections only on ports 80/443 to public global-unicast addresses; drop packets to every blocked range above before the allow rule; drop all other outbound protocols and destinations for that UID. The worker and controller use Unix sockets and receive no direct outbound-IP exception. No inbound rule is needed because the proxy and runner have Unix sockets only. If the existing ruleset, UFW backend, or rootless networking requires editing any current firewall table, stop and present the required diff before proceeding.

Do not expose or allow access to existing host listeners. The worker has no IP route to any of them. Do not add an allow rule for any existing listener or Docker subnet.

## Credential vault and secure import

Store authorized credentials in the vault only after owner review. The database contains account metadata and ciphertext only; encrypt each secret field using authenticated encryption with a unique nonce and record ID/version as associated data. Keep the master key out of the database and all containers except the trusted controller. Use `systemd-creds` with TPM2 sealing only when the target host supports and passes a restart/decryption proof; otherwise use systemd host-key encryption and document the root-compromise implications. Create an encrypted owner-held recovery copy of the vault key; never place that recovery key on the host.

The controller exposes owner/admin operations to add, update, list metadata, and revoke records. Logs contain only credential ID, operation, actor, timestamp and success/failure. The model and worker never receive secret bytes or the vault key. In future phases, account use occurs through brokered actions such as filling an approved browser form; Andrew does not get a `get_secret` tool. Revoking a vault record disables future broker use; revoke the site's active sessions separately where the platform supports it.

The vault stores each secret field with AES-256-GCM in SQLite. Its 256-bit key is stored as a systemd host-key-encrypted credential at `/etc/credstore.encrypted/andrew-vault-key`; the controller receives the decrypted key in its private systemd credential directory. TPM2 encryption was not validated: `systemd-creds has-tpm2` reported partial support. Host-key encryption does not protect against full host-root compromise or offline theft of both the credential and the host key stored on this machine. The encrypted key survives restart; recovery requires the host systemd credential key.

Import through the root-owned `/usr/local/sbin/andrew-vault-import` helper so the import process receives the same protected systemd credential as the controller. Run from an SSH TTY; `getpass` disables terminal echo and the values are never command arguments or environment variables:

```sh
sudo /usr/local/sbin/andrew-vault-import telegram_bot
```

Use the same command with `openrouter_api`, `x_username`, or `x_password`. Output reports only the credential label and success/failure. Do not paste credentials into chat, command text, `.env`, Markdown, logs, or Git.

## Phase 1 implementation sequence and acceptance checks

The administrator bootstrap and read-only firewall preflight have run. The following acceptance checks record their status; pending checks remain gates to wider access.

1. **Baseline and no-conflict record.** Privately capture a target-host baseline of service IDs, state, ports, networks, mounts, firewall and timestamps. Ensure proposed work changes none of the existing production resources.
2. **Service identities and directories.** Create and audit dedicated service users, subordinate ID ranges, root-owned units/config, and state/workspace directories. Verify ownership and group membership before enabling a worker.
3. **Firewall preflight.** Privately inspect the complete firewall state without making changes. Do not create a policy until a separate proxy design and rollback diff are reviewed.
4. **Rootless worker proof before model access.** Start a disposable test worker with no credentials. Confirm its network mode is `none`, its mount list matches the two entries above, and host files, host home directories, `.ssh`, website/bot data, container sockets/networks, loopback listeners and metadata endpoints are unreachable. Confirm a public HTTPS request succeeds only through the Unix proxy. Use a controlled DNS answer that resolves to a private test address and verify the proxy rejects it before connecting.
5. **Resource boundary proof.** In disposable workers only, test CPU, memory, PID, wall-time and workspace ceilings. Confirm the runner stops the worker at the configured limit and host services remain healthy. Do not stress or stop any existing service.
6. **Controller, vault and Letta.** Verify private service state, required encrypted credentials, agent/gateway integration and persistent database. Unit tests cover ciphertext storage, systemd credential loading, and conversation persistence. Do not put live service health or credential-validation results in this public plan.
7. **Model compatibility and budget.** Import the authorized OpenRouter credential only after the owner's confirmation. Make a single capped request to the configured primary model, verify one Letta tool call through the trusted controller, reconcile token/cost usage, and confirm the gateway refuses a request whose maximum possible cost exceeds the remaining daily/monthly allowance. Compare Gemini Flash only if its credential/provider setup is available and separately approved.
8. **Telegram owner controls.** Supply the Telegram owner ID and bot token through private deployment configuration. Verify owner-only `/status`, `/wake`, `/sleep`, `/pause`, `/activity`, `/diary`, `/budget`, `/help`, and rate limiting before enabling polling. Do not enable X browser login, posts, follows, DMs, email sending or registration without separate review.
9. **Restart and rollback.** Restart only new services and verify continuity of Letta state, vault entries, diary/workspace and event history. Test rollback by stopping only new units and confirming all existing containers, networks, listeners and mounts remain as baseline.

## Administrator privileges and production scope

The repository does not assert the state of any target host. Check administrator privileges, service accounts, encrypted credentials, and firewall state during private deployment. No worker, egress proxy, or firewall table should be activated until its boundary proofs pass. The full rootless worker proof remains a gate before enabling shell or browser tools.

Potential dependency risk: Ubuntu currently offers Podman 4.9.3 and related rootless packages, but the exact Letta deployment format and Qwen tool-call behavior must be pinned and tested before selecting image versions. Rootless containers share the host kernel, so a kernel/container escape remains a residual risk. If the networkless worker, socket policy, filesystem isolation or resource caps cannot be demonstrated, stop and recommend a separate VM/host.

## Owner inputs before activation

- Explicit confirmation of this plan before any system changes.
- Timezone and daily fallback time are private deployment configuration; Phase 1 creates no wake timer, and activation is deferred until all project phases are complete.
- Telegram owner user ID: private deployment configuration; use the configured owner as the sole sender for owner-only commands.
- Model usage starts with the configured primary model; evaluate Gemini Flash only as a controlled comparison. Re-check provider pricing before the first paid request.
- Budget period: calendar month; hard cap $10, protected reserve $1, approximate daily target $0.30. The desired 100–150 cycles/day is a soft target and is not guaranteed by that budget.
