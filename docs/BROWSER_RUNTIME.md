# Browser and sandbox runtime

## Owner authorization and status

The owner authorizes Andrew to browse public sites, log in and sign up without per-site or per-action approval prompts. This replaces the approval flow in the earlier browser design. Credential import supplies missing secrets; it is not an approval gate.

Browser/code runtime was installed and enabled from `f410a21` on **2026-10-15 at 01:06 UTC**. Dedicated service identities passed network isolation, resource limits, production workspace capacity, public browser control and controlled signup/login fixtures. The controller is active with the daily schedule and external tools enabled; owner pause/sleep, memory and spending state were preserved. Real third-party accounts and a live Qwen-directed browser task were not commissioning tests. Check [FEATURE_STATUS](FEATURE_STATUS.md) for the dated evidence and publication status.

## Commission this server

From an interactive SSH terminal, run the prepared installer in the checkout containing the matching `.artifacts/browser-runtime.oci.tar`:

```sh
sudo bash /home/daniel/andrew-browser-integration/scripts/enable-browser-runtime.sh
```

The shell used by the coding agent cannot authenticate sudo. The command supplies OS privilege for already-authorized work. It does not request per-site approval.

The installer verifies the release archive and browser source hashes, checks the content-addressed config ID after OCI import normalization and pins the resulting immutable manifest digest in both dedicated stores, provisions dedicated rootless launchers, installs a 10 GiB ext4 projects filesystem, applies only the `inet andrew_runtime` UID table, and tests through the installed private sockets. It tests direct networking/DNS denial, proxy success and failure, actual 2 GiB OOM kill, 256-task exhaustion, CPU throttling, the full 120-second deadline, real browser operations and controlled signup/login fixtures. It also runs the pinned isolated Letta callback harness without a paid provider.

Before the longer tests, a bounded preflight checks protected runtime-directory access and launches two fixed workers under each service identity. It requires workers to remain beneath the delegated service, retain their exact limits and leave the supervisor's cgroup unchanged. The services use `DelegateSubgroup=supervisor` and an explicit cgroupfs parent to avoid moving workers into a separate user-manager scope. Only each identity's own `/run/user/<UID>` is exposed beneath otherwise hidden home directories.

Only after acceptance does it stop the controller briefly, preserve its IDs/pause/sleep/cycle/spending evidence, install modules, write a root-owned code/image/policy proof, validate startup and enable the advertised tools. Letta and unrelated services are not restarted. Failure restores previous controller files/flags and Andrew firewall rules; dedicated storage and identities remain for retry. Evidence is private under `/var/lib/andrew/runtime-commissioning/`.

Dependencies include Podman, nftables, systemd 254 or newer (this server uses 255), ext4 tooling, ACL support, public HTTPS/DNS and the public-IP discovery endpoint. Missing ACL tooling is installed by the root command. Recommission after code, image, unit, policy or host network changes; a stale proof rejects external-runtime startup. The acceptance record is not an ongoing security monitor.

## Capabilities

- `python.run`, `node.run`, `shell.run`: disposable code workers, two CPUs, 2 GiB RAM, 256 tasks, 120 seconds, read-only root, no direct IP network, projects and proxy mounts only. Host output capture and concurrency are bounded. Container logging is disabled. Interrupted managed containers are removed at launcher startup; failed cleanup blocks new work.
- `browser.open` and `browser.control`: navigation, DOM snapshots, click/fill/select, keys, scrolling, tabs, screenshots, downloads, workspace uploads and context reset. The trusted browser has a separate Linux identity and no code-workspace, vault or controller mount. It shares neither authenticated profiles nor the private control socket with code workers. Browser lifetime is 120 seconds; saved credentials support fresh login afterward.
- `browser.account`: create a generated encrypted credential, list opaque account IDs, submit signup/login forms, revoke or delete. Creating a vault record does not prove site signup. Andrew must inspect the site result. Passwords travel only through private controller/browser RPC and are substituted into the exact-origin intended POST after page scripts process placeholders. Credential POSTs use a fresh private request context and cookie jar. Response bodies are replaced with an inert status object; response cookies are validated before installation, bound to the exact host and marked HttpOnly. Andrew must verify login on a subsequent authenticated GET page. Credential fetches disable redirects; same-origin 301/302/303 responses continue with a checked GET, while POST replay and cross-origin credential redirects are blocked. HTML URL-encoded and JavaScript JSON submissions are supported; JSON forms supply an explicit same-origin `submit_url`.

TLS verification stays enabled in production. The self-signed loopback fixture exception exists only in the test harness. All external network connections traverse the public-address proxy; special-purpose, local/connected and discovered public host addresses are excluded. Browser URLs use public HTTPS hostnames on port 443. This does not enable general host access.

Page/program output is untrusted data. Owner pause/sleep and budget checks still run before external tools; all model calls use the existing guarded gateway. No approval prompt is added to browser actions.

## Existing account import

Passwords must not be sent in chat. After commissioning, import an existing account through hidden terminal prompts:

```sh
sudo bash /home/daniel/andrew-browser-integration/scripts/andrew-site-account-import
```

This uses the systemd encrypted vault credential and prints only an opaque account ID. Generated signup passwords require no owner input. Revocation/deletion resets the browser context before changing the encrypted record, removing current authenticated cookies.

## Practical limits

This is a browser automation interface, not universal compatibility with every website. Human verification, CAPTCHA, MFA without an available factor, unsupported custom password transformations, login APIs requiring tokens only in the response body, JavaScript-readable credential-response cookies, and some federated authentication flows can block a login; Andrew must report that outcome. No verification bypass is implemented. Arbitrary JavaScript evaluation and WebSocket connections are not exposed. Chromium retains the previously owner-approved `--no-sandbox` setting inside the outer rootless boundary.

Screenshots require a reset after credential use because a hostile site could render a password echo into pixels. Downloads are limited to 512 KiB and stored as opaque artifacts in the controller's private directory; they are not automatically exposed to code workers. Uploads read only regular, bounded files beneath projects, without following symlinks. Artifact storage retains at most 32 files. Browser histories store encrypted sanitized visit metadata; attempted explicit navigation is recorded before network side effects and returned navigation metadata afterward.

## Repeat local checks

```sh
PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -q
ANDREW_LETTA_TEST_BINARY=/path/to/pinned/letta.js python3 -m unittest discover -s scripts/tests -q
python3 scripts/test-isolated-runtime.py "$(python3 -c 'import json; print(json.load(open("runtime/browser-release.json"))["image"])')"
python3 scripts/test-socket-activation.py "$(python3 -c 'import json; print(json.load(open("runtime/browser-release.json"))["image"])')" --full
```

The runtime commands use temporary sockets and the invoking user's rootless engine. The socket-activation test creates temporary native user units for the runner, browser and proxy, exercises the actual runner entrypoint, and removes its units afterward. The `--full` option additionally runs the full 120-second kernel/resource proof. Activation variables belong to the launcher listener and are removed from Podman child environments. These checks cannot establish the production UID firewall or 10 GiB mount proof. The real Chromium fixture runs inside a networkless disposable container and creates only temporary test accounts; no third-party account was created as a test artifact.


## Browser lifecycle serialization — REVIEW-007 source correction

Request execution, explicit close and idle reaping share one reentrant lifecycle
lock. Cleanup captures its process/container pair before clearing state, and a
new session cannot be installed until that cleanup finishes. The idle reaper
reserves server admission before scheduling its executor work and rechecks the
current session deadline under the lifecycle lock. A fresh session is not closed
by a stale idle decision. Busy requests fail cleanly without execution, then
admission resumes after cleanup. The acceptance installer now includes the
browser service module in its reviewed runtime-package digest and rollback set.
This is a source correction; production deployment remains separate.
