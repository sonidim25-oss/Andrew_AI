# Combined P1 and work-limit deployment

Owner authorized commit, push and deployment on 2026-10-17, including source
commits `18c05f7`, `d1660fa`, `22c80e0` and `7f2c1ff`.

The installer now validates 18 controller modules, including new `workers.py`.
It checks and replaces the reviewed runtime-service copies of `runner_service.py`,
`process_limits.py` and `runner_client.py` together, predicting the full package
digest. Original module bytes and presence, runtime files, runner config, runtime
acceptance check and proof are backed up privately before changes. The database
snapshot is evidence; rollback never overwrites live records with it.

The offline image is pinned to
`localhost/andrew-browser@sha256:3c641154407a1acc6d80316822f484629bd2fb6e24e3492f7d231395990c7849`.
Its config is `sha256:4acb90da8f237e371b3418ecfb733fc164a403bbe7d3785c3b146231f030d52c`.
The uncompressed OCI archive preserves that digest; its SHA256 is
`4d69fa3fc6796cf5edbbc19ffdd07c7bf48fcd44cd7324ea2c30df0e828dec89`.
A fresh isolated rootless store load and read-only networkless container verified
the image identity and all three embedded source hashes. Old images are retained.

Before stopping the controller, the installer verifies archive bytes and imports
the image independently under runtime and browser identities, then checks both
loaded identities. The root-owned private archive is opened by root and passed
through stdin, without granting either service access to its path.

The controller is stopped before runtime/config replacement. Runner and browser
restart against the new pinned image. Acceptance checks the existing CPU, memory,
PID, network and browser boundaries, isolated fake accounts and pinned fake-model
protocol. A controlled 130-second code execution must now finish successfully.
Only the predicted image, runner-config policy hash and runtime-package digest
may change before controller installation; unrelated bindings remain rejected.
The renewed proof includes the complete newly installed controller package.

After starting the controller, verify exact installed hashes, proof validity,
saved 6000-token allowance, new provider wait120, absent autonomous/worker
execution deadlines, services and schema. Compare messages, uncertainty records,
memory/personality/diary/research tables, usage/reservations and lifecycle IDs with
the stopped-controller snapshot. No wake, replay, real-model turn or live account
action is part of installation.

If failure occurs after a new-controller start attempt, stop and disable it before
restoring older code, runtime files, image config and proof. Leave the controller
stopped/disabled to prevent older delivery handling from repeating uncertain
messages. Failures before a new-controller attempt restore the previous bindings
and restart the unchanged controller. Do not remove either image as rollback.

Preparation tests: 317 core tests (315 passed, 2 optional skips). Installer tests
exercise unknown-source/image rejection, exact digest prediction, image identity
checks, asset/dependency rollback, active diary-claim refusal and guarded proof
renewal. All 69 script tests passed, including 37 installer tests and four image/config
asset tests. Live deployment evidence will be recorded after installation.


## Installation verified

Installed source `b792b7b` after pushing it to origin/main. Controller startup
completed at 00:13:06 UTC on 2026-10-17. All kernel/network/browser, fake-account,
per-UID networking and isolated Letta protocol checks passed; the real 130-second
execution completed successfully. Controller, Letta, runner and browser are
running with zero automatic restarts, and controller boot startup is enabled.
All 18 controller hashes, runtime dependencies, image/config/check bindings and
renewed runtime proof match. Saved Letta output allowance is 6000; provider socket
wait120 and absent autonomous/worker deadlines are verified in installed code.

Backup `/var/backups/andrew-incident-fiu2jy4q` contains acceptance and startup logs,
the stopped-controller SQLite snapshot and `deployment-verification.json`.
All 30 pre-existing tables retain identical rows; only the empty
`diary_delivery_claims` table was added. Spending remains 84 rows, USD 0.12036761;
four inputs remain held. Lifecycle/error state and conversation IDs are unchanged.
No production account operation, paid model call, wake or replay was initiated.
A new live autonomous turn was not exercised by installation.
