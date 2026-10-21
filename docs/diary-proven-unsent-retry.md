# Diary retry after a proven-unsent Telegram request

The deployed reply-retry fix correctly revokes an exact Telegram chunk admission
after a DNS failure, connection refusal or HTTP 429. Diary delivery nevertheless
left its entry uncertain forever, even after the network recovered. The new
regressions reproduce that behavior; six of seven initial cases failed before
the correction.

A diary sender now passes its original claim token into the shared delivery
helper. After a proven-unsent exception, the existing exact chunk/delivery/token
and no-receipt check revokes that admission. In the same SQLite transaction,
the store verifies the original diary sender claim, returns the entry to pending,
restores its attempt count and records its next eligible attempt. A changed
sender or a failed retry write rolls back the entire revocation.

Retry delay is 1, 2, 4, 8, 16, 32, then 60 seconds, capped at 60 seconds per
entry. Eligibility is checked both when listing entries and when claiming one.
The attempt count and deadline survive restart. Successful delivery removes the
backoff row. No thread sleeps inside a send or holds the database while waiting
for the deadline. Existing acknowledged parts and their receipts remain intact;
only the next proven-unsent part is attempted again, without a duplicate warning.

Timeouts, resets, invalid acknowledgements, uncertain sends, failed revocations
and changed sender claims remain held. Old uncertain diaries are not reset.
The existing explicit owner retry remains available. Restore continues to mark
pending deliveries uncertain. Reply delivery, media uploads, provider calls,
owner authority and conversation state are unchanged.

Validation: 105 focused tests passed, including ten new cases covering real DNS
classification, durable capped backoff, restart after revocation but before
sender release, multipart receipts, uncertainty after a later timeout, failed
revocation, failed retry persistence, changed sender token, competing stores and
shutdown. Existing receipt, multipart, ownership, lifecycle, backup and worker
checks passed. Existing media tests also confirm P3-19 is outdated: media is
deliberately uncapped, and concurrent reservation insertion is already atomic.

The coordinated package changes only controller diary.py, store.py and
telegram.py on the enabled ee089e6 spending-alert baseline; the runner package is unchanged.
Deployment is a separate administrator action while paused, with disk headroom
checked before acceptance. No live delivery or historical replay was performed.

Rebuilt on the installed spending-alert release ee089e6. Diary implementation
f3b6035 was applied as1251ffe without changing the spending-alert methods. A
combined regression verifies one alert remains queued after a diary DNS failure,
safe retry and database restart; no second alert is created. Controller, dispatch
and main files remain byte-identical to the installed alert release.
