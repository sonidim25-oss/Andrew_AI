# Andrew AI

Andrew is a self-hosted persistent AI agent prototype with a trusted Python controller, Letta integration, encrypted storage, lifecycle controls, and an isolated rootless Podman tool worker.

## Project status

This repository is an implementation workspace, not a deployment bundle. The controller, egress proxy, and worker services are not enabled by this repository. The worker image is built locally and has not been installed into the `andrew-runtime` service account's Podman store. Live HTTPS proxy access, automatic restart after host reboot, and account login/signup have not been verified.

The current work includes encrypted browser history with owner search/deletion commands, encrypted site-account storage primitives, exact-origin policy checks, and a disposable Python/Node/shell/browser worker. A trusted browser-control channel and owner workflows for real site registrations/logins remain unfinished. X and Gmail integrations are deferred. Autonomous schedules remain gated off.

Chromium currently runs with `--no-sandbox` inside the rootless, networkless, capability-free worker because nested user namespaces are unavailable in the development environment. The owner approved that setting for this outer isolation boundary. This reduces defense in depth and should be revisited where nested Chromium sandboxing is supported.

## Development checks

```sh
PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -q
systemd-analyze verify systemd/andrew-control.service systemd/andrew-egress.socket systemd/andrew-egress.service systemd/andrew-runner.socket systemd/andrew-runner.service
```

Runtime configuration, Telegram owner IDs, credentials, and host firewall inventories must be supplied and reviewed privately. Never commit them. See `PROJECT_DESCRIPTION.md` and `docs/superpowers/` for the design and implementation plans; check their status against the current code before deployment.
