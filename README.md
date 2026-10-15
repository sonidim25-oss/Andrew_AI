# Andrew AI

Andrew is a self-hosted persistent AI agent prototype with a trusted Python controller, Letta integration, encrypted storage, lifecycle controls, and an isolated rootless Podman tool worker.

## Project status

See [the verified feature status](docs/FEATURE_STATUS.md) for the dated distinction between source implementation, local test results, VPS installation, live integration checks, and autonomous enablement. Treat VPS state as a point-in-time snapshot and refresh it before deployment.

[Gmail setup](docs/GMAIL.md) covers the new OAuth integration: read-only initially, with separate owner controls for drafts, sending and moving messages to Trash. Its modules are installed; Gmail-specific testing and account consent remain pending.

The [daily lifecycle guide](docs/DAILY_LIFECYCLE.md) describes the conversation-and-memory release gate, preserved safety/budget limits, local tests and VPS commissioning command. Autonomous enablement is recorded separately from source readiness.

The [browser runtime guide](docs/BROWSER_RUNTIME.md) describes owner-authorized browser control, signup/login without approval prompts, credential import, isolation proofs and the administrator commissioning command. Source readiness is separate from installed enablement.

Chromium currently runs with `--no-sandbox` inside the rootless, networkless, capability-free worker because nested user namespaces are unavailable in the development environment. The owner approved that setting for this outer isolation boundary. This reduces defense in depth and should be revisited where nested Chromium sandboxing is supported.

## Development checks

```sh
PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -q
python3 -m unittest discover -s scripts/tests -q
systemd-analyze verify systemd/andrew-control.service systemd/andrew-egress.socket systemd/andrew-egress.service systemd/andrew-runner.socket systemd/andrew-runner.service
```

Runtime configuration, Telegram owner IDs, credentials, and host firewall inventories must be supplied and reviewed privately. Never commit them. See `PROJECT_DESCRIPTION.md` and `docs/superpowers/` for the design and implementation plans; check their status against the current code before deployment.
