# Stop new runner admissions after cleanup failure

P2-15 is confirmed at the socket admission layer. `RunnerService.run` already
refuses to launch containers when its health flag is false. The receipt server
nevertheless advertised `execution_enabled=true` and admitted new identities
after an unverified cleanup. Those unnecessary admissions could acquire grants
and create additional uncertain outcomes. Seven of eight initial real-socket
regressions failed against the prior implementation.

`RunnerSocketServer` now shares one availability check across capability replies,
receipt execution and legacy execution. It checks the existing storage-failure
flag, the runner's health and a service-lifetime failed-cleanup latch. A receipt
execution with unverified cleanup sets that latch before saving its uncertain
result. Changing worker flags cannot clear it within the same server owner.
Availability is rechecked after preparation, before receiver admission and
before starting work. A health change after durable admission remains uncertain;
no new evidence claiming non-execution is invented.

Lookup and duplicate requests for existing identities continue to return saved
evidence, including uncertain evidence. They do not acquire another grant or run
another worker. Ordinary program errors with verified cleanup remain normal
saved outcomes and do not disable new work. Busy behavior, controller authority,
receipt identities, admission fences, data retention and storage guards are
unchanged. The existing service startup recovery must succeed before a fresh
runner begins serving; this change adds no automatic restart or work replay.

Validation: 154 runner regressions passed, followed by one additional real
RunnerService/socket integration check. Eleven new tests cover failed cleanup,
truthful capabilities, rejection before grants/records, health changes during
preparation and on both sides of admission, old-result lookup, legacy requests,
ordinary program errors and service-lifetime latch behavior. The real runner
case uses actual RunnerService with an injected failed container-removal result;
no containers or external providers were invoked.

The source is based on diary release 7a11d54 and preserves the deployed daily
spending alert, diary backoff and earlier fixes. Only runner_service.py and
runner_receipt_server.py change in each coordinated package. There are no new
database tables, receipt formats, backend releases or worker images. Deployment
must follow completion of the diary rollout; source-package comparison is
against its exact proposed baseline until that rollout is observed enabled.
