# Andrew memory backup operations

The controller creates an encrypted SQLite snapshot at normal startup and every 24 hours while it runs. It retains the newest seven authenticated snapshots in `/var/lib/andrew/control/backups`. Snapshots include the controller database, including personality revisions, experience/interest records, activity references, finalized diary entries, and every private diary draft revision with its session and event references. Drafts are included by the existing whole-database SQLite backup; they do not have a separate backup system. Restoring an older snapshot without draft tables creates the empty additive schema on Store startup and preserves existing finalized entries.

The snapshot key is a separate random 32-byte key. `scripts/admin-setup.sh` stores it as `/etc/credstore.encrypted/andrew-memory-backup-key`, encrypted to the systemd host key. The plaintext key is supplied to a trusted one-shot or controller service credential directory and is not stored in the database directory. Do not replace or delete the encrypted key while snapshots must remain recoverable. The credential can be decrypted on the same host with root access and the existing systemd host key; it cannot be recovered after losing that host key.

Create a manual snapshot:

```bash
sudo andrew-memory-backup snapshot
```

List snapshot filenames without opening their contents:

```bash
sudo ls -1 /var/lib/andrew/control/backups
```

Restore a snapshot:

1. Keep the controller stopped. The restore command refuses while `andrew-control.service` is active.
2. Select a filename from the configured backup directory and run:

   ```bash
   sudo andrew-memory-backup restore memory-YYYYMMDDTHHMMSS.ffffffZ.andrewdb
   ```

3. The restore checks the authenticated encryption tag, SQLite integrity, and required diary and memory tables before atomically replacing `/var/lib/andrew/control/controller.sqlite3`. It refuses if SQLite WAL/SHM sidecar files remain beside the destination; inspect them before retrying.
4. Restoration quarantines inbound work, outgoing replies, diaries, media jobs and avatar proposals. The controller refuses to start operational work until billing and external actions are reconciled. A separate restore gate cannot be cleared by `/wake`.
5. When restoring over a healthy descendant database, the latest reasoning ledger, unsettled reservations and media ledger are retained. Conflicting histories are refused. Encrypted `restore-evidence-*.andrewdb` files beside the database preserve the pre-restore data for reconciliation and are not removed by daily snapshot rotation. If no healthy live database is available, reconcile billing externally before authorizing work.
6. Review delivery outcomes, provider charges, external account/actions and the separate Letta conversation. Quarantined records are never evidence that an action did not happen. After completing that review, explicitly acknowledge it while the controller remains stopped:

   ```bash
   sudo andrew-memory-backup reconcile --confirm BILLING_AND_ACTIONS_RECONCILED
   ```

   This removes only the recovery gate. Old queues stay held and Andrew stays paused; a subsequent owner wake is required for fresh work. Do not use `/replay` to guess whether older actions happened.
7. Install the updated `scripts/andrew-memory-backup` helper together with the controller modules when rolling out this release (`sudo install -o root -g root -m 0755 scripts/andrew-memory-backup /usr/local/sbin/andrew-memory-backup`). Run one-shot validation, start the controller, then wake only when ready.

Snapshots are local only. They protect against database corruption and mistaken deletion while the host and systemd host key are available. They do not protect against host loss, host-key loss, or a root compromise, and no off-host copy is configured.
