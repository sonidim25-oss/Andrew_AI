# Andrew memory backup operations

The controller creates an encrypted SQLite snapshot at normal startup and every 24 hours while it runs. It retains the newest seven authenticated snapshots in `/var/lib/andrew/control/backups`. Snapshots include the controller database, including personality revisions, experience/interest records, activity references, and Phase 3 diary entries.

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
4. Run the controller's one-shot validation command. Phase 4 does not start the controller service or enable autonomous/scheduled sessions.

Snapshots are local only. They protect against database corruption and mistaken deletion while the host and systemd host key are available. They do not protect against host loss, host-key loss, or a root compromise, and no off-host copy is configured.
