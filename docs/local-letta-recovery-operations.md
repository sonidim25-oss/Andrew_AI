# Local Letta recovery: review and operation

This change journals newly admitted local turns and resumes autonomous turns only
from confirmed controller receipts. It does not recover the older interrupted
input, infer success from transcript text, or retry an uncertain request/action.
Automatic recovery of owner replies is outside this increment. Existing legacy
input dispositions can prevent adoption of a conversation; that hold requires
separate reconciliation, without deleting the disposition records.

Production has not been changed during development. The owner selected personal
review of the final candidate. Local acceptance uses temporary stores and fake
provider/tool destinations. It cannot establish the current installed source or
service state; the root installer repeats the exact merge and acceptance against
that source before changing services.

## Review artifacts

The reviewed source is on branch `autonomy-media-20261017`. The separate backend
is pinned to `@letta-ai/letta-code` 0.34.13, pristine bundle SHA-256
`eab90b26ded2647769a00ae29ec95fea1881173da53e2d54f620fc6df085b2fe`.
The builder never changes the npm installation.

Local artifacts and private test logs live under:

```
/home/daniel/andrew-autonomy-media/.superpowers/sdd/2026-10-18-local-letta-durable-recovery/
```

`release-candidate/recovery-release.json` binds the complete backend artifact.
`candidate-accepted/candidate.json` binds the exact controller merge, test sources,
and backend manifest. This local candidate was merged with the reviewed
`f7418d2` source baseline; it is not represented as a current production export.
`candidate-accepted/acceptance.json` exists only after all four acceptance stages
pass. Their complete outputs are adjacent `*-acceptance.txt` files.

The process-loss suite runs the actual Node runtime and Python controller/model
worker in separate disposable processes. It kills them before/after durable
boundaries and checks provider/tool invocation counts, reservations, original
input identity, retained owner messages, and exact summary reuse. Other suites
cover ownership/fencing, malformed state and quotas, owner-control gates,
sequential turns, memory trust, paired restore, and isolated older-runtime startup.
Expected timeout/error logs from negative fixtures are not production incidents.
The two core skips and the recovery-only skips in pristine compatibility runs
must be reported separately from passing checks.

## Preview on the server

After reviewing the source and evidence, use a server terminal with sudo access.
This preview checks readiness, requires the existing idle/pause/sleep/error gate,
merges only recognized installed source hashes, and runs all acceptance checks.
It creates private evidence under `/var/backups/andrew-local-recovery-*` and does
not stop/start services:

```bash
cd /home/daniel/andrew-autonomy-media
sudo python3 scripts/install-letta-recovery.py \
  --backend-release /home/daniel/andrew-autonomy-media/.superpowers/sdd/2026-10-18-local-letta-durable-recovery/release-candidate
```

Unknown source, changed runtime proof, failed tests, or unsettled workers stop the
operation. Do not edit manifests to admit an unreviewed hash. If pause is needed,
use the existing owner control and let its active work settle first. The installer
does not wake, replay, clear reservations, or release held messages.

## Initial activation and enable

Adding `--apply` builds and validates a fresh candidate against the then-current
installation. It stops both workers, verifies they stopped, takes a paired private
snapshot, seals a root-owned separate backend under `/opt/andrew-letta-recovery`,
installs the controller merge, renews the existing code readiness proof, probes
capabilities, and checks service startup. Automatic recovery stays disabled.
Failures after stopping services leave them stopped, with evidence retained.

```bash
sudo python3 scripts/install-letta-recovery.py \
  --backend-release /home/daniel/andrew-autonomy-media/.superpowers/sdd/2026-10-18-local-letta-durable-recovery/release-candidate \
  --apply
```

Record the exact candidate directory printed by that successful invocation.
After checking its logs and completing the final review, enable using that path:

```bash
sudo python3 scripts/install-letta-recovery.py --enable /var/backups/andrew-local-recovery-REPLACE_WITH_ACTUAL_ID
```

Enable verifies the accepted candidate, installed code/backend, readiness proof,
idle state, and read-only capability handshake. It preserves owner controls and
does not request a wake. Enabling does not make legacy or unknown inputs safe.

For read-only diagnosis, use the existing diagnostic command:

```bash
sudo python3 scripts/diagnose-session.py
```

Recovery diagnostics contain fixed categories, IDs, phases and counts, without
raw prompts/results or tokens. A hold is a required outcome when proof is missing.

## Rollback and restore

Use the candidate directory from the successful installation:

```bash
sudo python3 scripts/install-letta-recovery.py --rollback /var/backups/andrew-local-recovery-REPLACE_WITH_ACTUAL_ID
```

Rollback first stops both services. It verifies the retained paired snapshot and
refuses older-runtime startup if that snapshot contains adoption markers,
nonempty/unknown input dispositions, reservations, or unproven queue state. A
refusal leaves services stopped. It never starts older code against the live
new-contract backend home. A permitted rollback restores prior controller code,
service overrides and readiness proof, uses an isolated copy of the proven
pre-admission backend snapshot, keeps the live journals/ledgers, and forces pause
and recovery-off. Its retained rollback override must be reconciled before a
later installation; a new activation refuses to silently reuse that override.

Paired snapshot/restore helpers in `andrewbot.memory_backup` are administrator
interfaces requiring both workers stopped. They bind both store identities and
file digests, reject active WAL/execution locks, retain pre-restore evidence and
preserve newer durable receipts. Restore installs separate backend/controller
reconciliation fences and pause. A controller-only memory backup is insufficient
to authorize recovery. The existing billing/action reconciliation command does
not clear the new local recovery fence. Fence release requires an explicit
operator reconciliation of both stores; this increment intentionally supplies no
automatic fence-clear command.
