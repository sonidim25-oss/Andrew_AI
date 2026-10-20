# Deployed source snapshot: 2026-10-20

The 119 captured application source files are recorded in [deployed-state.json](deployed-state.json), with their exact hashes and repository paths. The controller and runtime service packages match the enabled deployment adoption record. Backend extension source matches the installed pinned Letta release.

`prototype/andrewbot` is the deployed controller package. `runtime-services/andrewbot` and `runtime-services/checks` retain the exact installed service package and checks; these packages differ and must be deployed together through the recovery installer. `admin/verify-live-lifecycle.py` retains the installed commissioning helper. Newer maintained commissioning tooling is in `scripts/`.

The six tests already present on the host are archived in `installed-tests/`. Maintained tests, including the subsequent recovery regressions, remain in `prototype/tests/` and `scripts/tests/`.

Recovery installation and enablement succeeded, preserving owner pause. A delivered live reply and completed autonomous cycle remain unverified. P0 is still open.

The repository also includes the tested installer corrections and guarded repair tool used during installation. The root-owned accepted installation candidate remains immutable on the host. Runtime configuration, Telegram owner IDs, credentials, databases, provider receipts, raw logs and compiled dependency bundles are not part of this source snapshot. The backend is rebuilt from `letta_recovery/base.json` and `scripts/build-letta-recovery.py`.

Publication check counts and log hashes are recorded in [validation.json](validation.json).
