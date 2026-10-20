# Coordinated runner receipt recovery

This rollout covers newly journaled autonomous Python, Node and shell invocations.
It does not replay legacy uncertain attempts or claim that program output proves
an external publication or delivery. The controller, receiver and backend must
use the same reviewed candidate. An adopted installation cannot fall back to
unjournaled autonomous tools or use a feature-unaware rollback.

## Candidate preparation

`scripts/install-runner-recovery.py` stages both complete Python packages, the
pinned backend artifact and all acceptance inputs. Known source versions are
required for every changed module. Unknown deployed hashes stop preparation
before any service action. The manifest binds shared dependencies and installer
assets. Candidate sources and acceptance logs are hashed; later edits invalidate
the acceptance record.

A preparation against captured source is useful for offline validation. It does
not establish what is currently installed under `/opt`. Preparing against the
actual packages requires root, because the controller package is private.
Privileged preparation also runs the separate runtime UID credential probe with
a root-created inherited listener. The probe executes zero workers and rejects
the listener creator UID as proof of the runtime sender. Ordinary acceptance
cannot substitute a skip for this check.

From the isolated implementation checkout, prepare the exact host candidate:

```bash
sudo python3 scripts/install-runner-recovery.py \
  --prepare \
  --backend-release /home/daniel/andrew-autonomy-media/.superpowers/sdd/2026-10-19-runner-result-recovery/release-runner-receipts-task6b \
  --pristine-binary /tmp/andrew-letta-protocol-runtime/node_modules/@letta-ai/letta-code/letta.js \
  --directory /var/backups/andrew-runner-recovery-candidate
```

Preparation reads the installed packages and runs isolated tests. It does not
stop services, provision production credentials, change owner controls or call a
paid model. The destination must be new. Retain a failed candidate and its logs;
use a different destination for another preparation.

Review `candidate.json`, `acceptance.json`, source changes, fault-case logs and
the privileged UID result together. Independent whole-patch review and exact
host acceptance remain release gates. No review status should be inferred from
an earlier increment's acceptance.

## Installation and enablement

Andrew must be paused before installation. The installer preserves the current
owner revision, pause/sleep state, held messages and existing uncertain records.
Installation stops controller, backend, receiver and receiver socket. It checks
that the services and disposable workers have stopped, saves code and the old
service overrides, and takes paired and, where already provisioned, three-store
snapshots. It installs a durable systemd preflight fence before replacing code.

The receipt key is provisioned exactly once with `systemd-creds`. Existing
history without its original key, an unreadable credential, unsafe credential
paths or a fingerprint mismatch are refusals. The runner receives the key only
through `LoadCredentialEncrypted` and `CREDENTIALS_DIRECTORY`; it is never an
environment value or a worker mount. Production provisioning is part of actual
installation, not preparation.

After package installation the new receiver identity is pinned to the controller
and a verified three-store snapshot is retained. The root-owned adoption record
binds both code digests, persistent IDs, key fingerprint, immutable backend release
and service overrides. The existing runtime policy proof is renewed only if its
policy inputs and worker image are unchanged. Initial installation disables
receipt execution, starts only backend and receiver for capability inspection,
and leaves the controller stopped.

After exact candidate acceptance and final review, installation and enablement
are separate commands using the administrator-owned candidate directory:

```bash
sudo python3 scripts/install-runner-recovery.py --apply \
  --directory /var/backups/andrew-runner-recovery-candidate
sudo python3 scripts/install-runner-recovery.py --enable \
  --directory /var/backups/andrew-runner-recovery-candidate
```

Enablement requires the same owner controls, unchanged installed packages and
matching live receiver/backend capabilities. It preserves pause and never
requests a wake. Verify an explicit wake, a delivered owner reply and a completed
autonomous turn before closing this increment. Existing legacy uncertainties
remain retained and are never resolved by these tests.

A failure during installation or enablement leaves the durable installation
fence and affected services stopped. The phase, snapshots and current stores are
retained. Automatic rollback is refused; do not start older execution code.

## Backup and restore

`create_runner_recovery_snapshot` requires all workers stopped, a free receiver
owner lock, free backend execution locks, a checkpointed controller database and
no hot receiver journal. It binds all three identities, controller mappings,
imports, saved research links and encrypted receiver result hashes. Snapshots
contain no plaintext receipt key; their private databases still require private
backup storage.

`verify_runner_recovery_snapshot` checks the complete file hashes, identities,
controller links and receipt metadata. Restore additionally requires the original
receipt key and verifies authenticated encrypted responses. A wrong key,
foreign store, regressed admission or divergent immutable history refuses
replacement.

Restore retains all current stores before making changes, publishes a durable
external fence, and fences backend and receiver. Conversational memory may roll
back, while current verified backend/receiver execution histories remain current.
Newer controller mappings, imports, linked attempts, research observations,
held owner messages and owner revision survive. Restore disables receipt grants,
pauses Andrew and keeps the ordinary and local recovery holds. No restore fence
is cleared automatically, including after billing reconciliation.

The memory backup CLI exposes `runner-snapshot`, `runner-verify` and
`runner-restore` with explicit paths and `--workers-stopped`. Restore reads the
receipt key from a transient systemd credential, not from a command argument.
Legacy paired snapshots remain verifiable, but paired restore refuses a pinned
runner store; use the coordinated three-store API. A controller-only encrypted
memory restore also preserves newer runner evidence and disables execution.
