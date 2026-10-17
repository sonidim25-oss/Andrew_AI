# Andrew's optional skills

Status: deployed and verified from `b5d43fa`; image generation enabled, voice disabled. Initial scope follows the owner's design: three skills, a compact index, and on-demand instruction loading. Capabilities and permissions remain in the controller; preferences, experiences, and decisions remain in memory.

## Initial skills

| Skill | When useful | Scope |
| --- | --- | --- |
| `voice-communication` | Requested spoken replies or voice-delivery questions | Concise speech, current Eleven v3 voice configuration, spending authorization, and honest delivery status. |
| `image-creation` | Requested images, edits, or avatar concepts | Useful prompts, economical settings, authorized artifacts, and a private avatar proposal with separate owner approval. |
| `research-workflow` | Andrew chooses to start or resume an investigation | Hypotheses, observations versus interpretations, saved questions/findings/doubts/next steps, and accurate persistence claims. |

Canonical files live at `skills/<name>/SKILL.md`. Each explains purpose, actual tools, decision rules, constraints, and expected results. They do not assign Andrew topics, recurring duties, aesthetics, interests, or a personality. Remembering a preference does not create permission for spontaneous media.

A separate visual-identity skill is deferred; avatar guidance starts inside image creation. A system-self-check skill is deferred until controller diagnostics justify it. The research skill already explains why a new container, controller restart, and server reboot are different observations; it does not pretend the agent can inspect privileged host state.

## Discovery and loading

The standing prompt contains only the compact index and directions for two read-only tools:

- `skills.list({})` returns names and purpose summaries without reading full skill documents.
- `skills.load({"name": "research-workflow"})` returns one selected document and its SHA-256 version.

Andrew should load a relevant skill when useful, not load all of them on each wake. Previously loaded instructions may remain in his existing conversation history until normal compaction; the controller does not reinsert their full text at every wake.

The names are fixed to the three initial skills. There is no arbitrary path/URL, skill write/import, secret access, or skill-defined executable tool. The loader rejects extra arguments, unknown names, symlinked assets, modified documents, and documents over 12,000 bytes. It reports a fixed error without disclosing replacement contents or falling back to Andrew's writable workspace.

## Permission and memory boundaries

Loading a skill does not change media enablement, recipients, spending reservations, filesystem access, Gmail permissions, lifecycle state, or avatar approval. It does not make a hidden tool available. Existing controller authorization still governs every action.

In the current release, voice/image generation requires the existing explicit owner request flow. Skill text describes the implemented schemas and does not invent model-visible voice, quality, arbitrary-recipient, or profile-application parameters. Failed or uncertain external actions remain held without automatic replay. Image generation is owner-enabled through a persistent systemd drop-in; voice remains disabled for this rollout. Queued jobs now revalidate current permissions, per-job limits, daily/monthly usage, and their cost reservation before any provider call.

Research methods are optional. Reading skills performs no preference/personality write and makes no provider call. Existing operational tool auditing may record the read operation, just as it does for other controller tools. Andrew can choose when an experience or preference is worth saving through the existing memory tools.

## Packaging and persistence

The deployed loader uses `/opt/andrew/skills`, outside the untrusted runtime. Exact hashes of the three Markdown documents are embedded in the controller's versioned `skills.py` module, which is included in the runtime acceptance proof's code digest. The loader checks the document hash before returning any instructions.

The installer now prepares 21 controller modules plus all three skill documents. It verifies source document hashes and rejects unknown local edits in installed documents. Documents are installed under administrator-owned directories readable by the controller, with backups and rollback alongside the loader module. A failed installation restores earlier documents or removes newly introduced ones; existing agent data is never overwritten by a backup snapshot.

To change a skill, edit its canonical Markdown, update its hash in `skills.py`, refresh the tested deployment manifest, and run the relevant regressions. Do not edit a deployed skill in Andrew's workspace. His saved preferences and experiences survive independently in the existing controller/Letta persistence.

## Verification

Tests cover lazy metadata-only indexing, exact document bytes and versions, malformed names/arguments, path traversal, modified/oversized/symlinked documents, safe read access during lifecycle restrictions, no memory or spending grant, strict tool registration, source asset checks, installation, and rollback. The pinned isolated Letta fixture verifies a real `skills.load` callback with a fake provider; no paid requests or production messages are required.

Verification completed on 2026-10-17: core suite 369 tests (2 skipped), installer/protocol suite 80 tests passed, all 21 deployment source hashes matched, and whitespace checks passed. Independent review found no critical or important issues; the suggested partial-document-install rollback test was added and passed. Tests used isolated fixtures and a fake model provider; no production service, message, memory, spending record, or media setting was changed.

## Owner-authorized rollout

The owner requested commit, push, image enablement, and deployment. The compact index is written to the existing Letta agent system instructions at controller startup; it is not a separate owner message and makes no model request. Every subsequent turn can discover the three skill names and request a relevant document through the read-only tools. Restarting rebuilds this same index without deleting existing conversation history or memory.

The canonical Markdown is tracked in Git and the installer installs it under `/opt/andrew/skills`, not `/tmp` or a disposable container. Future deployments through `scripts/install-p1-fixes.py` include these assets, verify their versions, and roll them back with their loader on failure. Unknown local edits cause installation to stop rather than overwrite them. This protects normal restarts and deployments; rebuilding a server after disk loss still requires the repository and data backups.

Image enablement is a persistent administrator-owned systemd drop-in, versioned at `systemd/andrew-control.service.d/95-media.conf` and installed at the matching `/etc/systemd/system` path. Ordinary controller-code deployments do not remove it. It enables GPT Image 1 Mini while leaving voice disabled, with $3/month, $0.20/day, $0.10/job maximum reservations and two image jobs per UTC day. Owner `/image` and `/image_reply` requests remain required; autonomous generation is not enabled. No sample needs to be regenerated.

Pre-deployment verification for the image-enablement follow-up: 373 core tests (2 skipped), 80 installer/protocol tests passed. Independent review found no important issues and independently passed all 49 media tests. The four new budget/enablement tests reproduced the old failures before the fix. Production enablement and restart verification are recorded separately after installation.

## Deployment completed — 2026-10-17

Source `b5d43fa` was committed, pushed to `origin/feature/andrew-multimodal`, and installed through the acceptance installer. Evidence and the stopped-controller SQLite snapshot are in `/var/backups/andrew-incident-rabvratm`. Server runtime, browser account, UID network isolation, and pinned isolated Letta protocol acceptance passed. The persistent image-only drop-in was then installed, systemd reloaded, and controller startup verified again.

Post-deployment verification matched all 21 installed module hashes and validated the runtime proof and SQLite integrity. The controller user loaded all three exact skill versions from `/opt/andrew/skills`. A read-only Letta query confirmed the compact index in the existing agent's saved system instructions and its unchanged 6,000-token output allowance. Boot startup is enabled; all four services are active with zero automatic restarts. No server reboot was needed or performed.

Every row in all 33 database tables matches the pre-install snapshot, including lifecycle and conversation IDs, messages, held deliveries, memory, research, and spending. Both existing sample media artifacts retain their original checksums and delivered message IDs. No wake, replay, paid request, avatar update, or repeated sample delivery occurred. Private verification summary: `media-deployment-verification.json` in the evidence directory.

The running process confirms images enabled, voice disabled, and $3/month, $0.20/day, $0.10/job limits. The skill files and systemd setting reside on persistent server storage and remain part of the committed deployment source. Future updates must continue deploying the skill assets alongside their version-bound loader; they are not stored in Andrew's disposable execution container.
