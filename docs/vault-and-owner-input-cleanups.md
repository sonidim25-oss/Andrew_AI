# P3-21 and P3-22

P3-21 was implemented first in `a3ea1e8`. Vault access and account metadata
now compare `revoked_at` with the exact empty-text active sentinel. SQLite
accepts an empty BLOB in its TEXT column; the old truthiness check treated
that unexpected value as active. The regression reproduces that behavior
and verifies that only the expected marker permits access. Normal origin
binding, timestamp revocation and encryption remain unchanged. Both vault
copies are updated.

P3-22 was implemented next in `1d2a9be`. The pending-input check first looks
for queued/processing owner messages using a partial index. It uses an inbox
index on sender, chat and chat type, then streams candidate text until the
first model input rather than materializing the full inbox. It retains the
shared command classifier, prompt boundary, forwarding exclusions and claimed
message filter. It has no cache, page limit or stored classification that
could hide newly arriving input. Cursors close before leaving the store lock.
Existing databases gain the two indexes on normal Store initialization.

Synthetic benchmark, 10,000 rows per scenario, median of 30 checks:

| Scenario | Before | After |
|---|---:|---:|
| Unrelated inbox backlog | 3.1262 ms | 0.0190 ms |
| Ready owner message first in matching inbox | 34.9725 ms | 0.0327 ms |
| Delivered owner-message history | 1.7739 ms | 0.0193 ms |

These are local synthetic measurements, not production latency estimates.
An inbox containing only local commands still requires classification of the
eligible owner text; no behavior-changing approximation is used.

Validation: 84 related recovery/handoff/retry tests passed. A further 26 vault,
browser, routing and pending-input tests passed, including deterministic SQLite
instruction limits, preservation of inbox data when upgrading an existing
database, and visibility of another connection's new messages/claims.
The 18 coordinated installer tests passed. Packaging reconstruction matches
the enabled `dab41d5` Telegram retry deployment and changes only controller `store.py` and
`vault.py`, plus runner `vault.py`. No backend or worker-image changes.

The implementation is committed locally. Production installation is a
separate administrator step and requires Andrew to be paused before checks.

The initial rollout was safely refused after Telegram retry release `dab41d5`
was installed. Both changes are now applied on top of that deployed release
(commits `4e9ed5e` and `b432f0d`). Seventy-five combined vault/input/Telegram
reply/dispatch/handoff/transaction/installer checks pass. Reconstructed
controller and runner baseline digests match public adoption exactly.
The candidate still changes only store.py/vault.py in the controller and
vault.py in the runner; the installed Telegram fix is retained byte-for-byte
except for the separate pending-input additions to store.py.
