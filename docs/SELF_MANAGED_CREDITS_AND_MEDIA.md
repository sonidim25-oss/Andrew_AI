# Self-managed API credits and autonomous media

The owner authorized removal of every controller daily, monthly, reserve, per-job dollar cap, and daily media quota. Reasoning and media continue recording actual spending and uncertain reservations. These records do not restrict admission. API credits and provider-side key limits remain external facts, not controller allowances. Character and artifact size limits remain transport constraints.

Andrew decides how to allocate finite API credits and plan activity. `check_budget` and `/budget` report reasoning/media usage and safe provider credit metadata. The ordinary OpenRouter key endpoint reports key usage and any key allowance. Account credit totals require management permission; unavailable balances are reported as unknown, never unlimited. No model call is needed to read metadata. References: [current key](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key), [account credits](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits).

Standing owner permission enables `voice.generate(text, request_id)` and `image.generate(text, request_id)` in both conversations and autonomous turns. Each stable request ID identifies one immutable job; retries with the same ID do not repeat generation. Uncertain generation and delivery remain held. Background delivery sends to the owner only. `/voice_on` and `/voice_off`, also exposed as keyboard buttons, toggle spontaneous TTS. Explicit `/voice_reply`, `/voice`, `/image_reply`, and `/image` continue working. Image edits and profile changes retain their existing explicit flows.

The new deployment override enables the existing ElevenLabs Eleven v3 speech provider and GPT Image 1 Mini image provider. It switches TTS on and enables spontaneous voice and images. It does not require a voice command for each job. Choosing speech or images remains Andrew's decision; TTS does not force every text reply to become audio.

## Install

Send Andrew `/pause`, then run:

```sh
sudo python3 /home/daniel/andrew-autonomy-media/scripts/install-autonomy-media.py --apply
```

The installer validates the current acceptance record, merges only these changes into deployed code, and refuses overlapping edits. It checks exact proposed source hashes, tests the merged deployed code with fake providers and the pinned Letta protocol, reruns runtime/browser/network acceptance, backs up the original modules/configuration/database, and renews the runtime acceptance record. Core tests run against the merged code, not only the development checkout. Startup failure restores the previous code, configuration, and acceptance record.

The installer saves the owner's credit-management and autonomous-media guidance as a persistent memory record with `source=owner`, makes it available in the recurring memory context, and sets spontaneous TTS on. This is idempotent. Records of uncertainty and billing reservations are preserved. The installer preserves pause/sleep/error state and does not request wake or replay. After successful installation, use `/wake`, `/budget`, and `/media_status` to inspect the active behavior.

Account credit reads can require permissions unavailable to the existing inference key. The installer does not replace the inference credential or create a management key. Andrew can read the exact account balance whenever he chooses once an OpenRouter management credential is stored separately as `openrouter_credits`. Only the controller uses that credential for the credit-read endpoint; it is never exposed in model context or used for inference. The credential is read on demand, so importing or rotating it needs no controller restart.

After installing this release, check existing access:

```sh
sudo bash /home/daniel/andrew-autonomy-media/scripts/andrew-credit-access status
```

If OpenRouter reports management permission is required, create an OpenRouter management key in the account settings and import it through the hidden terminal prompt:

```sh
sudo bash /home/daniel/andrew-autonomy-media/scripts/andrew-credit-access put
```

Do not put the key in chat or in command arguments. The helper encrypts it in the existing vault and immediately verifies account balance access. Andrew can then call `check_budget` for the live balance with no per-check owner approval.
