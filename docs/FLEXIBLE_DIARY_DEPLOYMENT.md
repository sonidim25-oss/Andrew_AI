# Flexible diary deployment — 2026-10-23

Owner-authorized deployment completed and verified at **13:55 UTC**. The existing Andrew agent has the updated diary instructions. All four controller/backend/runner services and socket are active. **Andrew remains paused and asleep**, with the original sleep deadline and owner revision. No wake, model generation, diary publication or uncertain-action replay was requested during this deployment. His future autonomous diary choices have not been observed.

The implementation is `58f8f6d`; the exact installed-baseline binding and rollout configuration are `45dc7b6`. Branch: `fix/flexible-diary-20261023`, worktree `/home/daniel/andrew-flexible-diary-20261023`.

## Installed scope

Only six controller modules changed: `appserver.py`, `controller.py`, `diary.py`, `recovery_dispatch.py`, `sessions.py`, and `store.py`. This removes the diary prerequisite for sleep, adds private drafts with retained revisions, and makes publication an explicit immutable finalization. There is no daily writing obligation or fixed writing time.

| Component | Verified release |
| --- | --- |
| Previous controller | `a6c974edef7a6a95853b7c1c10bde235d32e7fe725680ec8c4c40cf4e77fe1a0` |
| Installed controller | `88db21a7152e1996a95d357742d070d3ce13ba008e3b15b4af28b5ae412a13ff` |
| Runner, unchanged | `5902ee5a385e07b97823c7065208115d0a4d2e347cd308b5e6161ce0636f60c6` |
| Backend, unchanged | `cd367258421ab6d4ed44e417c2dcc354b4c032795efb96ad80b0605d8a771e08` |

The existing guarded coordinated installer was used with the pinned installed baseline, clean committed source, owner pause/idle checks, restore barriers, disk headroom checks, privileged acceptance, disabled installation, capability checks and explicit enablement. The runtime acceptance proof is valid and no installation fence remains. The persistent agent's system prompt was compared directly with the installed source and matches exactly; agent and conversation identities were retained.

## Acceptance and preservation

- 91 Node tests passed.
- Source-identical core evidence was verified and reused: 885 tests run, 883 passed, two live-service tests skipped. No duplicate core suite was run for packaging.
- 327 script/integration tests passed against the installed backend release, using isolated temporary stores and deterministic providers.
- Pristine-runtime compatibility: 88 tests run, 18 passed, 70 expected feature skips.
- Service import and distinct service-user credential checks passed.
- Migration on a private database copy preserved all 62 existing tables and all seven finalized diaries, adding only the two empty draft tables.
- After the actual restart, every pre-existing table's row hash matched its pre-deployment hash. This includes finalized diaries/event references, memory/personality/research records, Telegram inbox/outbox/delivery evidence, usage/reservations, cycles and recovery receipts/holds. `diary_draft_revisions` and `diary_draft_event_refs` were the only added tables, both empty.
- Owner pause, sleep flag/deadline, owner revision, inactive session/cycle, autonomy-error state, restore controls and agent/conversation identities matched before and after deployment. No held input or reservation was removed or replayed.

The previous controller logged its conservative worker-shutdown warning during the controlled stop. It exited before migration; fresh controller startup succeeded at 13:54:53 UTC, all services are active, and the subsequent full data-preservation comparison passed.

## Backups and evidence

The private, root-owned coordinated release directory is:

`/var/backups/andrew-flexible-diary-20261023-candidate`

It retains the accepted immutable source, acceptance logs, code/adoption/override evidence, installation phase `enabled`, and these recovery snapshots:

- `three-store-before`
- `paired-before`
- `three-store-before-adoption`

Operator verification artifacts are under `/home/daniel/andrew-diary-preflight-20261023`: `baseline.json`, `core-evidence.json`, `migration-proof.json`, `database-before.json`, `verification.json`, `agent-prompt-verification.json`, and preparation/deployment logs. Database audit artifacts contain hashes and counts rather than diary/message content. The private migration-test database is not a production restore source.

The release used `scripts/diary_deployment.py --expected-commit 45dc7b6221694d96acf17f1a904ee39d23135300`. This is a historical execution record, not an instruction to rerun an already completed installation. The exact-baseline and prior-installation checks refuse an accidental repeat.
