# Browser and sandbox runtime

## Owner authorization and status

The owner authorizes Andrew to browse public sites, log in and sign up without per-site or per-action approval prompts. This replaces the approval flow in the earlier browser design. Credential import supplies missing secrets; it is not an approval gate.

Source and local proofs are available. The existing controller still runs the previous conversation/memory release with external tools disabled. Dedicated service identities, the UID firewall, production workspace capacity and autonomous browser enablement remain **pending administrator commissioning**. Check [FEATURE_STATUS](FEATURE_STATUS.md) for evidence.

## Commission this server

From an interactive SSH terminal, run the prepared installer in the checkout containing the matching `.artifacts/browser-runtime.oci.tar`:

```sh
sudo bash /home/daniel/andrew-browser-integration/scripts/enable-browser-runtime.sh
```

The shell used by the coding agent cannot authenticate sudo. The command supplies OS privilege for already-authorized work. It does not request per-site approval.

The installer verifies the release archive and browser source hashes, provisions dedicated rootless launchers, installs a 10 GiB ext4 projects filesystem, applies only the `inet andrew_runtime` UID table, and tests through the installed private sockets. It tests direct networking/DNS denial, proxy success and failure, actual 2 GiB OOM kill, 256-task exhaustion, CPU throttling, the full 120-second deadline, real browser operations and controlled signup/login fixtures. It also runs the pinned isolated Letta callback harness without a paid provider.

Only after acceptance does it stop the controller briefly, preserve its IDs/pause/sleep/cycle/spending evidence, install modules, write a root-owned code/image/policy proof, validate startup and enable the advertised tools. Letta and unrelated services are not restarted. Failure restores previous controller files/flags and Andrew firewall rules; dedicated storage and identities remain for retry. Evidence is private under `/var/lib/andrew/runtime-commissioning/`.

Dependencies include Podman, nftables, systemd, ext4 tooling, ACL support, public HTTPS/DNS and the public-IP discovery endpoint. Missing ACL tooling is installed by the root command. Recommission after code, image, unit, policy or host network changes; a stale proof rejects external-runtime startup. The acceptance record is not an ongoing security monitor.

## Capabilities

- `python.run`, `node.run`, `shell.run`: disposable code workers, two CPUs, 2 GiB RAM, 256 tasks, 120 seconds, read-only root, no direct IP network, projects and proxy mounts only. Host output capture and concurrency are bounded. Container logging is disabled. Interrupted managed containers are removed at launcher startup; failed cleanup blocks new work.
- `browser.open` and `browser.control`: navigation, DOM snapshots, click/fill/select, keys, scrolling, tabs, screenshots, downloads, workspace uploads and context reset. The trusted browser has a separate Linux identity and no code-workspace, vault or controller mount. It shares neither authenticated profiles nor the private control socket with code workers. Browser lifetime is 120 seconds; saved credentials support fresh login afterward.
- `browser.account`: create a generated encrypted credential, list opaque account IDs, submit signup/login forms, revoke or delete. Creating a vault record does not prove site signup. Andrew must inspect the site result. Passwords travel only through private controller/browser RPC and are substituted into the exact-origin intended POST after page scripts process placeholders. Credential fetches disable redirects; same-origin 301/302/303 responses continue with a checked GET, while POST replay and cross-origin credential redirects are blocked. HTML URL-encoded and JavaScript JSON submissions are supported; JSON forms supply an explicit same-origin `submit_url`.

TLS verification stays enabled in production. The self-signed loopback fixture exception exists only in the test harness. All external network connections traverse the public-address proxy; special-purpose, local/connected and discovered public host addresses are excluded. Browser URLs use public HTTPS hostnames on port 443. This does not enable general host access.

Page/program output is untrusted data. Owner pause/sleep and budget checks still run before external tools; all model calls use the existing guarded gateway. No approval prompt is added to browser actions.

## Existing account import

Passwords must not be sent in chat. After commissioning, import an existing account through hidden terminal prompts:

```sh
sudo bash /home/daniel/andrew-browser-integration/scripts/andrew-site-account-import
```

This uses the systemd encrypted vault credential and prints only an opaque account ID. Generated signup passwords require no owner input. Revocation/deletion resets the browser context before changing the encrypted record, removing current authenticated cookies.

## Practical limits

This is a browser automation interface, not universal compatibility with every website. Human verification, CAPTCHA, MFA without an available factor, unsupported custom password transformations, and some federated authentication flows can block a login; Andrew must report that outcome. No verification bypass is implemented. Arbitrary JavaScript evaluation and WebSocket connections are not exposed. Chromium retains the previously owner-approved `--no-sandbox` setting inside the outer rootless boundary.

Screenshots require a reset after credential use because a hostile site could render a password echo into pixels. Downloads are limited to 512 KiB and stored as opaque artifacts in the controller's private directory; they are not automatically exposed to code workers. Uploads read only regular, bounded files beneath projects, without following symlinks. Artifact storage retains at most 32 files. Browser histories store encrypted sanitized visit metadata; attempted explicit navigation is recorded before network side effects and returned navigation metadata afterward.

## Repeat local checks

```sh
PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -q
ANDREW_LETTA_TEST_BINARY=/path/to/pinned/letta.js python3 -m unittest discover -s scripts/tests -q
python3 scripts/test-isolated-runtime.py "$(python3 -c 'import json; print(json.load(open("runtime/browser-release.json"))["image"])')"
```

The last command uses temporary sockets and the invoking user's rootless engine. It cannot establish the production UID firewall or 10 GiB mount proof. The real Chromium fixture runs inside a networkless disposable container and creates only temporary test accounts; no third-party account was created as a test artifact.
