---
name: image-creation
description: Use for requested or autonomous images, edits, and private avatar proposals.
---

# Image creation

The owner authorizes you to create and send images spontaneously, including during autonomous activity. Check `media.status` for actual enablement. Decide when an image is worth creating; there is no assigned style or required activity.

Use `image.generate(text, request_id)` with a stable ID for each distinct job. During `/image_reply`, use the exact `owner_request_id` supplied by the controller instead. `/image <prompt>` already queues the exact prompt. The controller uses GPT Image 1 Mini through OpenRouter, generates one square image, validates and stores it, and delivers it to the owner only. There are no arbitrary recipients, URLs, filesystem paths, or provider credentials in these tools.

API credits are finite and shared with reasoning and voice. Inspect `check_budget` for provider credit metadata and `media.status` for spending. There are no controller daily, monthly, per-job dollar caps, or daily media quotas. You are responsible for allocating credits and planning activity. An unavailable balance is unknown, not unlimited.

Make prompts concrete and keep them within 2,000 characters. Do not include credentials or unnecessary private data. A job ID means queued, not delivered: inspect `media.get` before claiming delivery. Reusing the same request ID is idempotent. Never regenerate or resend an uncertain action with a new ID. Report uncertainty and wait for owner reconciliation.

Image editing retains the explicit `/image_edit <image_job_id> <instruction>` flow; `image.edit` acknowledges that saved request. Do not invent unsupported edit arguments.

An avatar is a separate publication decision. `profile.propose_avatar(job_id)` can propose an already delivered image. The owner must approve each profile change with `/avatar_approve <proposal_id>`. Spontaneous image permission does not authorize changing the profile. Guidance never overrides current tool permissions.
