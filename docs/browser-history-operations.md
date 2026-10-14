# Browser history operations

Browser visits are stored in `/var/lib/andrew/control/browser-history/history.sqlite3`. Each title and sanitized URL is encrypted with AES-GCM using the separate `andrew-history-key` systemd credential. Query strings and fragments are discarded before storage. Visits have no age-based expiry and are retained until an owner deletes them.

The database is deliberately excluded from the existing memory backup. There is no browser-history backup or restore path in this release. Loss of the history key makes encrypted titles and URLs unreadable. The history directory is separate from the worker workspace.

In the owner’s private Telegram chat:

- `/history [query]` searches up to 20 newest visits by title and URL.
- `/history delete <visit id>` deletes one visit.
- `/history delete all` starts a two-step deletion. Confirm with `/history confirm DELETE ALL HISTORY`.

Deletion compacts the SQLite database after removing rows. It does not delete copies made outside the service-managed history directory. No external backups should be made unless a future encrypted backup and deletion workflow is added.

The store has a 256 MiB default capacity. It never evicts earlier visits. When the database reaches its limit, recording fails and the browser navigation result must be withheld; the owner needs to remove history before recording can resume. History key provisioning and service restart remain deployment steps; the service will refuse startup when its history credential is missing or invalid.

The administrator setup and startup validation scripts invoke `scripts/provision-controller-data-keys.py` to provision a missing history key using systemd host-key encryption. It refuses to replace an existing credential or to create a new key when saved history records exist. Plaintext key bytes pass through process memory and pipes; only the encrypted credential is written to disk.
