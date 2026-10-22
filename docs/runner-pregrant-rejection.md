# Proven runner non-execution

A runner transport failure before a dispatch grant commits previously cancelled
the prepared mapping but left the model turn held as `tool_uncertain`. The
controller can now return a durable `runner_not_executed` tool error when it can
prove that the command was never admitted.

The live failure handler first commits the existing prepared cancellation. In a
separate transaction it rechecks the original dispatch authority, lease, fence,
owner revision, pause/sleep/restore guards, running cycle, and other recovery
outcomes. Only an I/O failure or the existing handshake deadline qualifies.
Protocol errors and failures after grant admission retain the existing hold.

The result has `executed: false` and a `runner_not_executed_v1` usage marker tied
to the immutable operation identity. The mapping stays cancelled, with no
admission timestamp; the legacy attempt stays `not_executed`. This is a
controller cancellation receipt, with no receiver execution receipt or import.
Reading it revalidates those facts. Repeated callbacks and restart reuse the
saved result without another runner RPC. Failed receipt persistence leaves the
cancelled attempt held. Older cancellations without this receipt stay held.

Diagnostics record fixed stage, category, and admission state only. They exclude
command text, exception text, credentials, and message bodies. No execution
deadline, spending cap, automatic replay, or historical state repair is added.

## October 22 incident

Owner-supplied read-only evidence identifies the 22:16:53 UTC shell attempt as
cancelled before admission: the receiver was prepared, the mapping had no
admission timestamp, and the legacy outcome was `not_executed`. The old exception
handler discarded the underlying exception, so its original transport or
protocol cause cannot be recovered from that evidence. This change covers
verified transport failures in future live attempts; it does not claim to
identify that lost exception or clear the existing autonomy error.

The implementation branch starts at `b8e0a7f`. Later budget, timezone, and memory
packages must retain this change when integrated; deploying this branch alone
over a newer installation would omit those later changes. The shared socket
module is included in both controller and runner package manifests. Deployment
requires a newly built, reviewed package against the then-current installation.

## Verification

- Full core suite: 851 tests, successful (2 optional checks skipped).
- Scripts suite: 289 tests, successful (95 optional runtime checks skipped).
- All 8 runner backend protocol tests passed against the installed immutable
  backend release `3f5cfd7afcd1f30844a0fb186179331a5fbc6b823d52952b1fbb3487515c33c7`.
- Independent review found the EOF classification and shared-module manifest
  pin issues; both were fixed and reviewed again with no remaining blockers.

These checks use disposable fixtures. They do not constitute privileged
deployment acceptance, a live recovery, or a new completed Andrew cycle.

## Deployment package

The `deploy/runner-pregrant-rejection-20261022` branch integrates the implementation
with the installed P2-10 package `0c41bb3`; the original implementation commit
remains available on `fix/runner-pregrant-rejection`. Pending timezone and memory
packages are not installed and are not included here.

The rollout entry point is `scripts/pregrant_deployment.py`, which reuses the
reviewed budget deployment guards. The owner launcher pins this deployment
commit. It requires the exact enabled P2-10 baseline, pause and quiescence, no
restore barriers, and snapshot headroom before acceptance. Acceptance reuses
the passing core log only for identical inputs and still requires the remaining
privileged checks. Saved complete acceptance is verified on rerun. A prior
installation record requires inspection rather than automatic overwrite.

Expected installed controller digest:
`680776f2e8d7f0adf75d84605fba866a9b048d3514ebb52e307d6d611271aade`.
Expected installed runner digest:
`5902ee5a385e07b97823c7065208115d0a4d2e347cd308b5e6161ce0636f60c6`.
The backend release remains `3f5cfd7a...`. Only three controller modules and the
shared runner socket module change from the installed P2-10 package.

The rollout preserves owner controls, reservations, and billing, checks all four
services/socket after enable, and does not wake Andrew, replay messages, or clear
the existing error. Later timezone/memory rollout packages must be rebuilt
against this new baseline after deployment; their older baseline pins will refuse.
