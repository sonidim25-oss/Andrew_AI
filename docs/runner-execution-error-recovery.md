# Completed execution errors are recoverable results

On 2026-10-21 at 12:43:29 UTC, Andrew held a `python.run` invocation even
though its runner receipt contained a durably saved `execution_failed` result
with verified cleanup. `ReceiptEvidence.importable` required `status=ok`,
so normal delivery and subsequent evidence-only reconciliation both rejected
the recorded program error. The voice-generation calls preceding it had
confirmed controller receipts.

The importer now also accepts the exact pair `status=error` and
`safe_error=execution_failed` when the receipt is in `result` state, its
response is present, and cleanup is verified. Existing authenticated identity,
digest, mapping, atomic settlement, and backend confirmation checks still
apply. The returned text remains `Status: error`, including stderr and
artifact references. A failed program may already have performed work; the
original operation is never dispatched again.

Timeout, cancellation, infrastructure errors, unknown outcomes, failed cleanup,
and conflicting evidence remain held. This change does not clear owner
controls, release reservations, replay held messages, or reset the conversation.
The existing coordinator imports evidence, confirms the exact backend tool,
and checks owner controls and pending input before continuing.

Validation: the new normal-delivery and lost-reply regressions failed before
the correction. All 144 runner tests and 18 coordinated installer tests passed.
Six actual isolated pinned-backend cases passed, including normal delivery of
a program error and lost-error recovery after restart; both prove one worker
execution and delivery of the saved error to the next model step.

Production requires the coordinated installer for both copies of
`runner_protocol.py`. No backend release or worker image changes are needed.
