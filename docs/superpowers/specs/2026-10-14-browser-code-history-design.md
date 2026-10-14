# Browser, Code Runtime, and Persistent Visit History

**Status:** Design approved in conversation; awaiting owner review of this written spec.
**Date:** 2026-10-14
**Scope:** First implementation slice of Andrew's browser and online identity capabilities. X and Gmail integrations are deferred.

## Intent and success criteria

Andrew needs broad public web reading through an isolated browser, and Python and Node.js execution with outbound access to any public HTTPS destination through a controlled proxy. He should be able to retain a useful record of sites he visited and use credentials for a site again after signup. The owner wants browser history and account records retained indefinitely until the owner deletes them.

Success means that browser and code tools work without exposing the host, existing services, controller state, browser records, or credentials to arbitrary code; public HTTPS works only through the policy proxy; browser activity has durable owner-searchable history; and account secrets can be saved and used without entering model context, code output, or ordinary logs.

This spec describes the first architectural slice only. It does not implement X or Gmail. It does not start scheduled autonomous sessions. Existing project gates for an observed live session remain in force.

## Existing project constraints

The project already defines separate `andrew-control`, `andrew-runtime`, and `andrew-egress` identities, a rootless Podman worker, a Unix-socket egress proxy, a workspace-only mount, and no direct worker networking. The planned worker ceilings are 2 CPU cores, 2 GiB RAM, 256 processes, 120 seconds per shell request, and 10 GiB of workspace. Browser concurrency is one session with an initial 2 GiB memory ceiling. Phase 1 acceptance checks for worker isolation, proxy behavior, and firewall policy are still incomplete; browser and code tools must remain unavailable until those checks pass.

The current App Server adapter explicitly does not expose Browser or Bash tools. This work adds the browser and runtime as controller-mediated tools; it does not grant the Letta process or model a host shell, network socket, credential API, or container runtime socket.

## Architecture

Use three cooperating boundaries:

1. **Trusted controller and broker.** The controller validates requests, applies domain and action permissions, records audit events, talks to the egress proxy, and brokers credential operations. It never returns raw credential values to the model or worker.
2. **Ephemeral rootless runtime.** A short-lived Podman worker runs Python, Node.js, shell utilities, Chromium, and Playwright. It has only the Andrew workspace mounted read/write and one Unix-socket proxy capability. It has no IP network, host service mounts, controller state, history database, credential database, or container socket.
3. **Encrypted persistence.** A dedicated encrypted store holds sanitized visit records and account metadata/secret fields. It is separate from autobiographical memory, diary, objective audit logs, and the workspace. The trusted controller alone can decrypt account secrets. Browser cookies and transient session state are not the durable credential mechanism; future logins use brokered credentials.

The browser can read public pages. Python and Node.js can make arbitrary public HTTPS requests through the proxy. Proxy policy resolves destinations itself, rejects private, reserved, link-local, loopback, metadata, and non-global destinations, pins validated addresses, and repeats validation across redirects. No direct DNS or IP path is available from the worker. Web page content and tool output are untrusted data and cannot override system or controller policy.

**Owner-approved Chromium exception (2026-10-14):** this environment denies nested user namespaces, and Chromium would not start with its internal sandbox enabled. The owner explicitly approved running Chromium with `--no-sandbox` inside the rootless Podman worker. The outer worker remains unprivileged, `--network=none`, read-only except its isolated projects mount and proxy socket, capability-free, resource-limited, and short-lived. This lowers defense in depth against a browser exploit; keep the explicit boundary proof and do not expand mounts or networking to compensate.

## Permissions and action boundaries

- Public browsing and reading are enabled after isolation acceptance checks pass.
- Python and Node.js may use any public HTTPS destination through the proxy. Requests are subject to resource ceilings, rate/byte limits, and content-free audit records. Because broad HTTPS also permits uploads, the mounted workspace must exclude diaries, memory, browser history, vault data, controller state, and unrelated host files.
- Read access does not imply permission to create accounts, submit posts, send messages, or change remote data. Such actions are allowed only on an exact owner-approved domain after its current rules have been reviewed and permit the action. The owner may authorize activity within that reviewed domain scope without confirming each individual click.
- Do not bypass CAPTCHAs, rate limits, identity checks, email verification, or anti-bot controls. Pause for an owner-operated human step or abandon the action.
- Purchases, payments, financial transactions, and payment-account creation are outside scope.
- X and Gmail are deferred. X actions must use an approved API integration when separately configured; X's current automation policy prohibits scripted automation of the website and places additional limits on automated interactions. [X automation rules](https://help.x.com/en/rules-and-policies/x-automation?lang=browser). Gmail will require a separately reviewed OAuth integration with minimum scopes; Google classifies `gmail.readonly` as restricted and `gmail.send` as sensitive. [Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes), [OAuth verification guidance](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification).

## Visit history and account persistence

For each browser navigation, retain timestamp, page title, and a sanitized URL with query and fragment removed. Do not save page bodies, form contents, passwords, cookies, or authorization headers as history. The history is encrypted at rest, searchable by the owner, and retained indefinitely until the owner deletes it. Provide owner controls to search and delete individual records or the full history. Deletion must remove the record from the active store and account for any project backups through a documented deletion path.

Account records contain site origin/domain, username or account identifier, creation/last-use timestamps, and encrypted secret fields. Store secrets in the existing controller-owned encrypted credential vault, extending its schema and broker only through a narrow, origin-bound interface. The model can request a login to an approved origin; the broker fills credentials directly into the isolated browser session. Secret bytes must never be exposed to model prompts, Python/Node processes, shell arguments, environment variables, diary, Telegram, Git, or ordinary logs. Owner controls must support listing metadata, updating, revoking, and deleting credentials. Any signup flow must save the generated credential through the broker before declaring success.

Do not store browser password-manager data or persistent cookies as an alternate secret store. Use an ephemeral isolated browser profile and brokered login so credential revocation takes effect for future logins. Revoking a credential does not automatically invalidate a site's existing session; where available, provide a separate owner-directed sign-out/revoke-session action.

The history and account stores are separate from Andrew's personal memory. Memory may reference a visit or account by record ID when useful, but must not copy passwords or private page contents. At storage capacity, stop adding history and alert the owner; never silently evict old records. The owner may free space by deleting records.

## Failure handling and rollout

- If the proxy, worker isolation, or destination validation is unavailable, fail closed and disable browser/code requests. Do not fall back to direct networking.
- If history storage is unavailable, pause navigation that would create a record and notify the owner; do not continue invisibly without recording.
- If the credential broker cannot verify the requested origin or account record, refuse autofill and do not return the secret to the caller.
- If signup reaches a human verification step, pause with a clear owner request. If the owner does not complete it, abandon the signup without claiming success.
- Roll out in order: prove Phase 1 rootless worker and proxy isolation; add read-only browser and history; add Python/Node with proxy egress; then add reviewed-domain credential signup/login. Keep outbound posting/messaging and X/Gmail disabled until their separate integrations and permissions are approved.
- Do not enable scheduled autonomous sessions until a manually observed 5–10 minute session succeeds. This spec does not activate any service or schedule.

## Verification requirements for the later implementation plan

The plan must include acceptance checks that demonstrate:

1. Worker network is `none`; direct DNS, direct public HTTPS, private/LAN/loopback/metadata destinations, host listeners, and existing container networks are unreachable.
2. Public HTTPS works through the proxy, while private/reserved DNS answers and redirects are rejected before connection.
3. Worker mounts contain only the dedicated workspace and proxy socket; credentials, history, diary/memory, controller files, and existing services are inaccessible.
4. CPU, memory, process, execution-time, and workspace limits are enforced by the runner.
5. Browser navigation records sanitized URL/title/time durably across process and host restart; query strings/fragments, page bodies, form data, and secret values are absent.
6. Owner search and delete work, including the full-history delete path; no automatic retention expiry occurs.
7. Credential encryption survives restart; origin mismatch, missing approval, revoked credentials, and broker failures refuse autofill. Tests and audit inspection prove secrets do not enter model input, worker output, process arguments, environment, or ordinary logs.
8. Unapproved domains and actions outside the approved scope are blocked; human verification pauses the workflow.

These are implementation acceptance requirements, not results claimed by this design document. Do not run tests or enable services until an implementation plan is approved.

## Explicitly deferred

- Gmail OAuth, X API/app setup, and their account-specific capabilities.
- Public posting, direct messaging, email sending, and social automation beyond a separately reviewed domain/action grant.
- Long-lived browser cookies or a browser profile shared with the owner.
- Scheduled autonomous sessions and any live service activation.
- Changes to existing Docker, website, Telegram bot, firewall, or host-wide networking configuration beyond the separately reviewed Andrew-scoped proxy policy.
