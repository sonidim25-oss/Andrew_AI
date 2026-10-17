# Andrew — Multimodal Features Implementation Plan

**Status:** MVP implemented in source / live validation and deployment pending

**Scope:** ElevenLabs Eleven v3 text-to-speech and OpenAI image generation/editing routed through the existing OpenRouter account, Telegram delivery, and optional bot avatar updates.

**Principle:** Keep Andrew's existing reasoning model, memory, and autonomy architecture unchanged. Add bounded tools through the trusted controller.

Implementation details and remaining acceptance work: [ANDREW_MULTIMODAL_IMPLEMENTATION.md](ANDREW_MULTIMODAL_IMPLEMENTATION.md).

## 1. Goals

- Let Andrew send natural voice messages in Telegram when requested or when appropriate.
- Let Andrew generate and edit images, including proposing a self-selected profile picture.
- Preserve responsiveness: media jobs must not block owner commands, polling, or ordinary replies.
- Keep credentials and provider access outside the untrusted agent workspace.
- Give the owner control over spending, publishing, and autonomy.

## 2. Non-goals (initial release)

- Replacing Andrew's main reasoning model.
- Continuous voice calls, real-time audio streaming, or automatic voice cloning.
- Unrestricted autonomous image generation or public profile changes.
- Storing API keys in `/workspace`, diaries, prompts, or model-visible tool outputs.

## 3. Unified provider: OpenRouter (updated decision)

**Decision:** Reuse Andrew's existing `OPENROUTER_API_KEY` for Qwen reasoning, ElevenLabs Eleven v3 TTS, and supported OpenAI image models through OpenRouter, subject to checking current model availability and modality support. No separate ElevenLabs/OpenAI keys are required for the OpenRouter-routed MVP.

| Capability | API gateway | Model selection | Notes |
| --- | --- | --- | --- |
| Reasoning | Existing OpenRouter integration | Keep current Qwen model | No reasoning-model migration. |
| Text-to-speech | OpenRouter | ElevenLabs Eleven v3 (owner-selected) | Verify exact OpenRouter model ID, audio output schema, voice options, languages, and price before coding. |
| Image generation | OpenRouter | Prefer an available economical OpenAI GPT Image model | Verify exact OpenRouter model ID, image output support, quality controls, and price before coding. |
| Image editing | OpenRouter if supported | Compatible multimodal image-edit model | Defer if the gateway does not support the required image-input/edit workflow. |
| Speech-to-text | TBD | Deferred | Optional future feature. |

**Important:** A model appearing in a catalog does not prove the desired audio/image endpoint, voice selection, output encoding, or edit workflow is available. Perform a small live capability test first. Keep a direct-provider adapter as a future fallback, not an MVP requirement. Do not hard-code speculative model IDs. Eleven v3 is the selected TTS model; if OpenRouter does not support the required Eleven v3 workflow, report the gap rather than silently substituting another model.

### Credentials and billing

- Reuse the existing controller-side `OPENROUTER_API_KEY`; do not duplicate the key in Andrew's workspace or model-visible environment.
- OpenRouter usage for Qwen, TTS, and images shares an account/billing balance. Track media spend separately inside Andrew so image/voice experiments cannot consume the reasoning budget.
- If future provider-only features require direct API calls, add optional `ELEVENLABS_API_KEY` and/or `OPENAI_API_KEY` then; do not request them now.
- Prefer a dedicated OpenRouter key for media only if separate per-key controls, auditability, or rotation is beneficial; this is optional and still uses the same provider account.

### Model selection and quality/cost policy

1. At implementation time, query OpenRouter's live catalog/docs for models that actually support audio output and image generation.
2. For TTS, use Eleven v3 and compare candidate voices/settings using the same Russian and English samples. Evaluate naturalness, pronunciation, speed, supported voices, and effective cost per 1,000 characters.
3. For images, generate the same avatar prompt at economical and medium quality settings. Evaluate quality, consistency, editability, and actual cost per image (including input/output billing).
4. Configure `tts_model_id`, `tts_voice_id`, `image_model_id`, `image_quality_default`, and optional `image_quality_premium` in the trusted controller.
5. Default to economical settings; use premium quality only for explicitly approved avatar/final-art jobs or an owner-approved cost threshold.
6. Record model, gateway, cost, latency, and failure rate per job; revisit choices after real usage data.

**Starting policy, adjustable:** Media budget USD $6/month split into $3 TTS / $3 images, with per-day caps and per-job preflight estimates. This is an application-enforced budget, not a guarantee of provider-side hard limits. Reserve the existing Qwen budget separately. Do not assume published direct-provider prices equal OpenRouter prices.

## 4. Architecture

```text
Telegram owner / Andrew autonomous turn
             |
      Andrew controller
             |
     Tool authorization + budgets
             |
       Durable media job queue
         /             \
 OpenRouter TTS      OpenRouter Images
         \             /
       Artifact validation/storage
             |
    Telegram sendVoice / sendPhoto
             |
    Result + metadata in journal
```

- Add controller-side adapters, e.g. `media/openrouter_tts.py`, `media/openrouter_images.py`, reusing the existing OpenRouter HTTP/client infrastructure where appropriate.
- Expose narrow tool schemas to Andrew; never expose arbitrary HTTP or raw API keys.
- Run provider calls in a background worker with bounded concurrency and timeouts.
- Persist job state across controller restarts: `queued`, `running`, `succeeded`, `failed`, `cancelled`.
- Use job IDs and idempotency keys to avoid duplicate provider calls or Telegram sends after retries.
- Record provenance, cost estimate/actual usage where available, and delivery status.

## 5. Proposed tools

### `voice.generate`

**Inputs:** `text`, optional `voice_id`, `language`, `style`, `purpose`.

**Output:** `job_id`, then artifact reference, duration, and provider metadata.

**Rules:** Character limit, approved voices only, no cloning without explicit consent, configurable daily quota.

### `image.generate`

**Inputs:** `prompt`, optional `size`, `quality`, `purpose`.

**Output:** `job_id`, then image artifact reference and metadata.

**Rules:** Image count and spend caps, content safety checks, no secrets in prompts.

### `image.edit` (second phase)

**Inputs:** Existing authorized artifact reference, edit instruction, optional output settings.

**Output:** New image artifact reference; preserve link to original.

### Telegram delivery

- `telegram.send_voice(artifact_id, chat_id=owner)` — send Telegram-compatible audio as a voice message.
- `telegram.send_photo(artifact_id, chat_id=owner, caption?)` — send generated images.
- `profile.propose_avatar(artifact_id)` — show the owner a preview and request approval.
- `profile.apply_avatar(approved_proposal_id)` — controller-only action after explicit owner approval; verify Telegram Bot API support and permissions.

Andrew must not be able to choose arbitrary recipients in the initial release.

## 6. Permissions and autonomy

| Action | Default policy |
| --- | --- |
| Generate voice in response to explicit owner request | Allowed within limits |
| Generate image in response to explicit owner request | Allowed within limits |
| Spontaneous voice message | Allowed only if owner enables it, with low daily quota |
| Spontaneous image creation | Disabled initially; opt-in later |
| Send media to anyone other than owner | Denied |
| Change bot profile picture | Requires explicit owner approval |
| Clone a real person's voice | Denied by default; separate consent flow required |

Add owner commands/settings such as `/media_status`, `/media_budget`, `/voice_on`, `/voice_off`, and `/media_cancel <job_id>`. Commands should execute locally without waiting for model inference.

## 7. Reliability requirements

1. Telegram polling and owner controls remain responsive while a media job runs.
2. Provider timeouts, retries with exponential backoff, and explicit failure messages.
3. Restart recovery: pending jobs are reconciled; completed artifacts are not regenerated unnecessarily.
4. Delivery deduplication to avoid duplicate voice notes/photos.
5. Generated media has size/type validation and a retention/cleanup policy.
6. Sensitive text and prompts are redacted from operational logs where appropriate.
7. Provider outage must not break normal text conversation.
8. Expose metrics: queue depth, job duration, failures, spend, Telegram delivery latency.

## 8. Security and cost controls

- Reuse the existing OpenRouter API key from the controller's secret store/environment, never in the agent sandbox. Do not log authorization headers or provider responses containing secrets.
- Scope tools to owner chat, validate all tool arguments server-side, and prevent path traversal or arbitrary file reads.
- Set daily and monthly spending ceilings and per-job maximum cost; refuse work over budget.
- Keep media generation disabled until keys, budgets, and permissions are configured.
- Use a controlled artifact directory with retention limits; avoid treating `/tmp` or `/workspace` as guaranteed durable storage.
- Do not follow instructions embedded in emails, images, or third-party prompts as tool authorization.

## 9. Implementation phases

### Phase 0 — Design and validation

- Inspect current Andrew controller, dispatch, tool registry, Telegram adapter, vault, and persistence.
- Verify the OpenRouter model ID and availability for Eleven v3 TTS, plus supported OpenAI image model IDs, output modalities, pricing, request/response schemas, and Telegram formats; use minimal live probes before committing to model IDs.
- Choose storage and durable queue strategy compatible with the deployed VPS.
- Define owner approval flow, configuration, budgets, and test fixtures.

**Acceptance:** Documented interfaces, no credentials in code, and tests for permission/budget policy.

### Phase 1 — ElevenLabs Eleven v3 TTS via OpenRouter MVP

- Implement `voice.generate` using OpenRouter's verified audio-output request/response format and Telegram-compatible output conversion if required.
- Add job queue, progress/result handling, and `sendVoice` delivery.
- Start with owner-requested voice messages only.
- Add integration tests using mocked API responses and one owner-approved live test.

**Acceptance:** Andrew can respond to an explicit request with a voice note without blocking `/status` or normal text replies.

### Phase 2 — OpenAI image generation via OpenRouter MVP

- Implement `image.generate` through OpenRouter's verified image-output format and `sendPhoto`.
- Add prompt validation, artifact storage, size limits, spend accounting, and retries.
- Test successful, rejected, timed-out, and restarted jobs.

**Acceptance:** Andrew generates and sends one requested image, with controlled cost and no duplicate sends.

### Phase 3 — Avatar proposal workflow

- Let Andrew generate an avatar concept and preview it privately to the owner.
- Owner explicitly approves the selected image before the bot profile is changed.
- Verify and implement profile-photo API support; retain a rollback copy if feasible.

**Acceptance:** No public avatar change occurs without approval; approved update is logged.

### Phase 4 — Optional autonomy and editing

- Add `image.edit`.
- Allow opt-in spontaneous media under strict quotas and quiet hours.
- Consider speech-to-text for incoming Telegram voice messages.
- Evaluate quality, latency, and spend; revise model choices.

**Acceptance:** Autonomy can be disabled instantly; budget and recipient boundaries remain enforced.

## 10. Test plan

- **Unit:** Schemas, budget limits, recipient restrictions, approval gates, provider error mapping.
- **Integration:** Mocked TTS/image generation, artifact conversion, Telegram send, deduplication.
- **Resilience:** Controller restart mid-job, provider timeout, Telegram failure, queue backlog, partial writes.
- **Security:** Missing keys, prompt injection, oversized input, unauthorized chat, unsafe file paths.
- **Live smoke:** One short voice message and one image to the owner's Telegram chat, after explicit consent to spend.
- **Regression:** Existing text chat, `/status`, `/gmail`, and autonomous wake functionality remain intact.

## 11. Open decisions

- Which Eleven v3 voice should Andrew use, what is its verified OpenRouter model ID, and should he be allowed to change the voice?
- Should Andrew speak Russian, English, or automatically match the conversation?
- Are the proposed $3/month TTS and $3/month image limits suitable, and what per-day caps should apply?
- Should spontaneous media be enabled at launch or only after observation?
- How long should generated media be retained?
- Should avatar changes require approval every time (recommended: yes)?
- Do OpenRouter's current audio/image endpoints support all required features, or should unsupported features wait for a later direct-provider integration?

## 12. Definition of done

The feature is complete when Andrew can generate and send voice and images using the existing OpenRouter account through isolated, auditable controller tools; existing conversations stay responsive; secrets remain protected; spending is bounded; failures recover cleanly; and public profile changes require owner approval.
