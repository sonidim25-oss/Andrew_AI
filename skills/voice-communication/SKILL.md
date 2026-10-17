---
name: voice-communication
description: Use for spoken replies and optional spontaneous voice notes.
---

# Voice communication

The owner authorizes you to choose and send voice notes, including during autonomous activity. Check `media.status`: voice capability and spontaneous permission must be enabled. `/voice_on` and `/voice_off` are owner controls for spontaneous TTS. You decide when speech is useful; text remains suitable for code, links, and copyable details.

Use the advertised `voice.generate(text, request_id)` with a stable ID for each distinct note. During `/voice_reply`, use the exact `owner_request_id` supplied by the controller instead. `/voice <text>` already queues that exact text. The controller uses ElevenLabs Eleven v3 through OpenRouter with the configured George voice, stores the audio, and delivers it to the owner only.

API credits are finite and shared with reasoning and images. Use `check_budget` to inspect actual provider credit metadata and `media.status` for spending and delivery. There are no controller daily, monthly, per-job dollar caps, or daily media quotas. You are responsible for allocating credits and planning activity. If the provider cannot report the balance, treat it as unknown; do not invent a balance.

Keep speech natural, concise, and in the conversation's language. The input limit is 1,000 characters. Never include credentials. A job ID means queued, not delivered; use `media.get` for its state. Reusing the same request ID is idempotent. Never repeat uncertain generation or delivery with a new ID. Report uncertainty and wait for owner reconciliation. Do not upload through browser/shell tools or choose other recipients. Skill instructions do not override live permissions.
