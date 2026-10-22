P2-16: cleanup after signal/wait failures

Reproduced with real subprocesses and injected cleanup failures: a killpg error
skipped process.wait entirely, and TimeoutExpired/OSError from the final wait
skipped pipe closure. Three regressions failed on the previous implementation.

Both process_limits.py copies now attempt the existing five-second wait even
when signalling fails, and close pipes in an outer finally when signalling or
waiting raises. Errors still propagate; a failed wait is never reported as a
successful ProcessResult. No execution deadlines, retries, background reaper,
runner ownership or container-cleanup policy were introduced or changed.

Tests cover a signal failure followed by actual child exit/reaping, and a final
wait timeout or OSError with all pipes closed. Test teardown explicitly reaps
its children even on failing implementations. Existing process-capture and
unlimited-work tests pass. This fixes the reproduced missing cleanup steps; it
does not guarantee immediate reaping when the OS cannot terminate or wait for a
child. The five-second cleanup timeout still raises in that case.

Validation: 3 new cleanup + 5 existing capture + 10 unlimited-work tests pass in
the controller package; the 8 cleanup/capture checks also pass against the
runtime-services copy. No production services or data changed.

Deployment packaging uses the verified installed diary timezone release 428adc0.
The coordinated manifest includes process_limits.py for both roles, and exact
package reconstruction changes only that module in each package. Root rollout
requires owner pause and snapshot disk headroom before required acceptance.
Owner controls and existing uncertain work are preserved; no wake is requested.
