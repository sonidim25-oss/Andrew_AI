# Andrew media implementation

Status: source commit `b5d43fa` deployed and verified; image generation enabled, voice disabled. Approved voice and image samples remain delivered; queued-budget revalidation finding 1 is fixed. The existing Qwen model, token allowance, lifecycle policy, memory records, message replay policy, and reasoning spending ledger are unchanged.

## Owner commands

| Command | Behavior |
| --- | --- |
| `/voice <text>` | Queue Eleven v3 speech of the supplied text without model inference. |
| `/voice_reply <question>` | Ask Andrew for a spoken answer; grants one voice job in this owner turn. |
| `/image <prompt>` | Queue one economical GPT Image 1 Mini image without model inference. |
| `/image_reply <request>` | Let Andrew compose an image prompt; grants one image job in this owner turn. |
| `/image_edit <image_job_id> <instruction>` | Edit a previously delivered generated image. |
| `/media_status`, `/media_budget` | Local job states, reservations, limits, and avatar outcomes. |
| `/media_cancel <job_id>` | Cancel an unstarted job and release its reservation. Already attempted jobs remain held/accounted. |
| `/avatar_propose <image_job_id>` | Propose an image whose preview was delivered. Andrew can also use `profile.propose_avatar`. |
| `/avatar_approve <proposal_id>` | Explicitly authorize background publication of that image as the bot avatar. |
| `/voice_on`, `/voice_off` | Explain that spontaneous media remains disabled in this release. |

All commands require the existing authenticated private owner chat and reject forwarded commands. Ordinary conversation does not grant media spending. `/voice_reply` and `/image_reply` use the model worker, so they cannot occupy the local command thread. When asleep or budget-blocked their text follows the existing saved-message policy; later processing does not restore a spending grant. Use a fresh explicit request after waking Andrew if media is still wanted.

The tools `voice.generate`, `image.generate`, and `image.edit` accept only the controller-provided owner request ID. A turn-scoped authorization permits one matching job, then expires. Otherwise they can only acknowledge an existing exact-content owner job. `media.status` and `media.get` expose metadata, not artifact paths or credentials. Recipients are fixed to the owner. Profile application is not an agent tool.

## Provider verification (2026-10-17)

Read-only public catalog requests confirmed:

- `elevenlabs/eleven-v3`: speech output; premade `george` voice is supported. The gateway returns raw MP3 bytes from `POST /api/v1/audio/speech`. Default voice is George; supported configured alternatives are Brian, Daniel, Adam, Sarah, Alice, and Jessica. No cloning or arbitrary provider parameters.
- `openai/gpt-image-1-mini`: image output and image references; `POST /api/v1/images`. Use one image, low quality, square aspect ratio. Its endpoint advertises neither `size` nor `output_format`, so neither is sent. Only validated PNG results are accepted; unexpected formats are held and never regenerated automatically.
- The speech catalog showed $0.00004/character during a temporary promotion; reservations use the regular $0.00008/character rate. Unknown actual speech cost is conservatively charged internally at the reservation.
- Image endpoint pricing: $0.000002/input text token, $0.0000025/input image token, $0.000008/output image token. The default per-job reservation is $0.10, including editing. Actual reported image cost replaces the reservation. This estimate requires confirmation by live probes and periodic price review; it is not a provider hard cap.

Sources: [TTS API](https://openrouter.ai/docs/guides/overview/multimodal/tts), [Eleven v3](https://openrouter.ai/elevenlabs/eleven-v3), [Image API](https://openrouter.ai/docs/guides/overview/multimodal/image-generation), [image model endpoint](https://openrouter.ai/api/v1/images/models/openai/gpt-image-1-mini/endpoints), [Telegram media and profile APIs](https://core.telegram.org/bots/api).

No paid generation, production Telegram delivery, avatar publication, wake, or uncertain-message replay was performed during implementation. Public catalog checks verify advertised capability, not account-specific successful generation.

## Configuration and limits

The trusted controller reuses the existing encrypted vault `openrouter_api` record. No media key enters the agent runtime.

| Setting | Default |
| --- | --- |
| `ANDREW_MEDIA_ENABLED` | `0`; only `1` enables media |
| `ANDREW_MEDIA_VOICE_ENABLED` | `1` by default; deployed override `0` keeps voice disabled while images are enabled |
| `ANDREW_MEDIA_CAPABILITIES_VERIFIED` | `0`; must be `1` before enabling after approved live probes |
| `ANDREW_TTS_VOICE` | `george` |
| `ANDREW_MEDIA_MONTHLY_PER_KIND_USD` | `3` each for voice and images |
| `ANDREW_MEDIA_DAILY_PER_KIND_USD` | `0.20` each |
| `ANDREW_MEDIA_VOICE_JOB_USD` | `0.08` |
| `ANDREW_MEDIA_IMAGE_JOB_USD` | `0.10` |
| `ANDREW_MEDIA_VOICE_CHARACTER_USD` | `0.00008` |
| `ANDREW_MEDIA_RETENTION_DAYS` | `30` |

Additional bounds: 1,000 speech characters; 2,000 image prompt characters; 20 voice jobs/day; 2 image jobs/day; 8 MiB/artifact; 256 MiB total artifact directory; validated PNG limited to 2048×2048 pixels. Budgets use UTC periods and atomic SQLite reservations. Reasoning usage remains in the original `usage` ledger; media usage stays in `media_jobs`.

The provider socket timeout is 120 seconds. Telegram media uploads use 30 seconds in the independent media worker. Avatar conversion requires `/usr/bin/ffmpeg`, uses only pipe input/output, and has a 10-second execution timeout. These limits apply to media; the owner's existing unlimited autonomous/code work settings are preserved.

## Persistence and failure behavior

Two additive tables in the controller database store jobs and avatar proposals. Artifacts reside at `<ANDREW_DATA_DIR>/media-artifacts`, normally `/var/lib/andrew/control/media-artifacts`, outside disposable containers. Files are written privately, fsynced, and atomically renamed before success is recorded. DB backups include job metadata; the existing DB-only backup does not include artifact bytes, so disaster recovery still needs a filesystem backup of this directory.

The worker serializes media calls separately from polling, owner controls, text reply senders, and model work. A durable claim precedes every provider request, media delivery, and avatar application. On restart, in-flight claims become `uncertain`; they are never automatically replayed. Their potential costs remain reserved. Same-UTC-day queued jobs can proceed; unstarted jobs from an earlier day expire with a local owner notice so old reservations cannot bypass a new spending period; succeeded jobs with pending delivery reuse the saved artifact. Acknowledged deliveries are not sent twice. Provider exceptions are sanitized and prompts/auth headers are excluded from operational logs.

Retries with backoff are intentionally withheld for attempted provider requests and Telegram sends because neither provider-side idempotency nor an unambiguous failed delivery has been established. The owner can inspect results and explicitly submit a new request; changing request IDs is not an automatic recovery action.

Old delivered artifacts expire after the configured retention period, while spending/audit records remain. Avatar proposal images and images referenced by pending/uncertain edits are retained. Held artifacts are not silently removed. Media cancellation currently applies to queued work; attempted calls cannot be safely undone.

The existing installer now includes both media modules, preserves baseline module hashes for upgrade verification, and refuses installation while media work is pending or active. Its rollback tests cover removing newly added media modules. The installer is prepared but was not run.

## Remaining acceptance work

- Explicitly approved paid smoke tests: short Russian and English voice samples, a generated image and an edit, delivered only to the owner; record cost, quality, format, and latency. Compare voices/settings and low/medium image quality before choosing final settings.
- Deploy the tested code with media disabled first; enable only after capability/account checks and configured budgets. Publishing an avatar requires its own owner command every time.
- Independent review could not run because the review agent hit its usage limit. A separate author review and automated tests are available; fresh review remains outstanding.
- Optional spontaneous media, quiet-hour policy, speech-to-text, premium-quality selection, and direct-provider fallback remain future work. No arbitrary-recipient delivery or voice cloning was added.

## Verification

Final source verification: core suite 345 tests run, 343 passed and two optional tests skipped; script suite 72 passed with the pinned isolated Letta binary. The script suite includes a real Letta media-tool callback against a fake local provider and no production credentials. All 28 media unit/integration regressions passed. All 20 deployment source hashes match; `git diff --check` passed.

Changes are prepared on `feature/andrew-multimodal`; deployment is pending. Existing changes, including the requested deletion of `plan.md`, were preserved. Production services and production databases were not modified by this implementation.

## Approved live sample — 2026-10-17 02:00 UTC

The owner approved one short bilingual voice sample and one low-quality image, estimated under $0.15 total. Exactly one provider request per sample was made; no generation or delivery was replayed.

- Eleven v3 / George: valid MP3, 6.269 seconds, 100,771 bytes; Telegram delivery acknowledged as message 162. Job `fba7574f4f044c6c8f39807a11b905bf`. Actual provider cost was not returned; conservative accounting is $0.00624.
- GPT Image 1 Mini / low: valid 1024×1024 PNG, 1,301,426 bytes; provider reported $0.002242. Job `c4f0f37a95da4a8cbaff8794514761f6`. Telegram delivery was attempted but not acknowledged, so it remains `uncertain` and held. The original artifact is retained; no regeneration or resend was performed.
- Combined image charge plus conservative voice accounting: $0.008482. Exact combined billed cost remains unavailable because the speech response omitted cost.

Jobs, spending, artifacts, and the report are durable in the existing controller database, media-artifacts directory, and `/var/lib/andrew/control/media-sample-20261017.json`. Only the new approved media records were added; existing messages, memory, lifecycle, and reasoning-spend rows were preserved. Automatic media generation remains disabled and the feature has not been deployed.

The sample runner `scripts/media-live-sample.py` defaults to free preflight checks and requires `--approved-sample` to attempt the approved jobs. Its fixed authorization IDs and durable claims prevent replay. A mocked regression verified that running it again neither regenerates artifacts nor resends an uncertain delivery. That regression passed.

Read-only Telegram diagnostics accepted small and image-sized multipart requests; the cause of the unacknowledged image send is not established. Image delivery acceptance therefore remains pending explicit owner verification or authorization to resend the saved artifact.

At 02:16:29 UTC the owner explicitly authorized one resend of the saved image. Telegram acknowledged it as message 163. No new image generation was performed and no additional generation cost was incurred. The original unconfirmed attempt and this explicit authorization remain recorded; the durable image job now reads `delivered`. Both approved samples have therefore reached Telegram with acknowledgements. Media remains disabled in the deployed controller until a separate code deployment and enablement.

## Selected review repairs

Findings 2–4 are now fixed in source; see [ANDREW_MEDIA_REVIEW_FIXES.md](ANDREW_MEDIA_REVIEW_FIXES.md). Shutdown admission, retention reference races, and silent held deliveries have regression coverage and a clean independent scoped review. Current verification: 360 core tests passed with two optional skips, and 73 script tests passed. The reduced-budget revalidation finding (1) remains unresolved and still blocks enablement. No repairs have been deployed.

## Deployment verified — 2026-10-17

Deployed source commit `81ee6be` from `feature/andrew-multimodal`, with media disabled. Controller startup: Sat 2026-10-17 02:38:59 UTC. All 20 installed controller module hashes match the tested manifest, and the renewed runtime acceptance proof is valid. Kernel/network/browser, fake-account, isolated Letta protocol, and the 130-second sandbox execution checks passed.

Controller, Letta, runner, and browser services are active with zero automatic restarts; controller boot startup is enabled. Comparison against the stopped-controller SQLite snapshot confirms all 33 existing tables have identical rows, including messages, memory, research, uncertainty records, reasoning/media spending, lifecycle, and sample delivery acknowledgements. Both generated sample files retain their original hashes. The 6000-token allowance, provider wait of 120 seconds, and removed autonomous/code execution deadlines remain verified.

Evidence and backups: `/var/backups/andrew-incident-mlgyoqvh`, including `controller-before.sqlite3`, `deployment-verification.json`, `media-deployment-verification.json`, startup logs, and artifact hashes. No wake, uncertain-message replay, paid request, new media delivery, or avatar update was performed during deployment. Finding 1 remains unresolved; deployment does not enable media.

## Image enablement verified — 2026-10-17

The owner-authorized rollout installed `b5d43fa` and the persistent `95-media.conf` systemd drop-in. Running policy verifies images enabled, voice disabled, monthly $3, daily $0.20, per-image reservation $0.10; two image jobs/day remains enforced. Use `/image <prompt>` for a direct request or `/image_reply <request>` for an Andrew-composed prompt during an available model turn. Avatar changes still require separate approval. Verification preserved all 33 database tables and both existing sample artifacts; no paid request, wake, or replay occurred. Full evidence and skill-discovery details: `docs/ANDREW_SKILLS.md`, backup `/var/backups/andrew-incident-rabvratm`.
