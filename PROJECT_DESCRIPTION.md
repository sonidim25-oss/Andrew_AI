# Digital Life — PROJECT_DESCRIPTION.md

Version: 1.7 (Phase 4 prototype implementation status)
Status: Phase 4 memory continuity is implemented in the workspace prototype; the controller remains disabled and production deployment/live model-switch validation are pending
Language: Russian for owner-facing communication; code and internal identifiers may use English.

## Owner-approved amendment — 2026-10-13

This amendment supersedes conflicting earlier requirements in this document. The owner approved Letta + a trusted controller + a separate rootless agent runtime, then explicitly authorized the Phase 1 foundation. The current deployment status is recorded below; future system changes still follow the approved scope and production safeguards.

### Public repository deployment note

This repository contains implementation source and design material, not a live host inventory. Deployment status, Telegram owner ID, credentials, provider configuration, network ranges, and service health must be verified privately on the target host. Keep those values out of source control.

The design uses an encrypted credential vault and keeps secrets out of Git, ordinary Markdown, logs, model context, and unrestricted worker processes. Account integrations remain disabled until their broker and owner-review workflows are complete.

### Phase 4 prototype implementation status (2026-10-14)

- The workspace prototype now stores append-only dated personality revisions and experience/interest records in controller SQLite. FTS5 search returns bounded records with event and diary provenance. The current personality projection is injected by the trusted gateway as ephemeral system context, and strict Letta callbacks can search, add records, or create revisions. The founding letter and safety constraints have no agent edit tool.
- Encrypted AES-256-GCM snapshots use SQLite's online backup API, run at normal controller startup and every 24 hours, and retain the newest seven authenticated files. A root-operated command supports manual snapshots and restoring only a named file after the controller is stopped. The backup key is separate from the credential vault key and encrypted with the systemd host key. Snapshots remain on the same host; losing the host or host key loses recovery capability.
- `ANDREW_MODEL_ID`, `ANDREW_MODEL_INPUT_USD_PER_MILLION`, and `ANDREW_MODEL_OUTPUT_USD_PER_MILLION` configure a single validated model/rate record shared by OpenRouter, the gateway, controller estimates, and Letta. Existing Letta agent updates use its loopback agent update endpoint and do not rewrite SQLite memory.
- These changes are in the workspace prototype; they have not been installed into `/opt/andrew/prototype` or applied to the running VPS. Offline tests cover the changed paths. Live Letta model update was not exercised. `ALL_PROJECT_PHASES_COMPLETE` remains false; the controller, autonomous sessions, and scheduled wakes remain disabled.
- Operator steps and backup limits are documented in `docs/memory-backup-operations.md`.

## 1. Vision

Build a self-hosted autonomous AI character living on the owner's existing Linux VPS. The agent is aware that it is an AI operating in a Linux environment. The owner supplies an initial personality, but the agent is free to choose activities, develop interests, revise beliefs and pursue or abandon projects based on accumulated experience. This is an experiment in persistent autonomous behavior, NOT a productivity assistant and NOT a claim of consciousness.

The agent should have at least one guaranteed daily wake opportunity, but no mandatory 2–3-hour continuous work shift. It chooses when to work, pause, sleep, and request another wake-up within owner-defined resource limits. Sleeping incurs no model calls and preserves state. The owner may manually wake it for an additional session at any time. The agent has web access, a browser, a shell, its own writable filesystem and development tools. It maintains a first-person daily diary delivered through Telegram, and the owner can chat with it and inspect activity during its active periods.

## 2. Environment and constraints

- Target: existing Linux VPS hosting a small website and several Telegram bots. Codex CLI is already installed on the VPS; owner connects over SSH from a personal computer.
- Deployment must not disrupt or modify existing services, ports, containers, volumes, firewall rules or reverse proxy configuration without explicit approval.
- Agent runs in an isolated, unprivileged runtime, preferably a separate VM if practical; otherwise a hardened rootless container with documented residual risks. No host root access, Docker socket, host mounts, privileged mode or access to existing service secrets.
- Agent may read public websites and write/run code inside its assigned sandbox. Owner-authorized account credentials may be stored in the encrypted vault, but are only used through narrow controller tools; they are never exposed to the agent's arbitrary shell. Public X/Twitter pages only where technically and legally accessible; do not evade authentication, anti-bot restrictions or paywalls.
- API budget is **USD $10 per calendar month**. Enforce budget and per-session cost controls outside the agent, not only through its prompt. Allow configurable provider-side spending limits where available. The agent must never see the provider's API key or Telegram bot token.
- Do not assume specific VPS CPU/RAM, Linux distribution, architecture, virtualization support or Docker installation; inspect before deciding.

## 3. Core components

### 3.1 Agent runtime and memory

- Evaluate **Letta (formerly MemGPT)** as the initial framework for persistent state, memory blocks, conversation history and tools. Verify its current self-hosting requirements, supported models, tool calling, database needs and deployment compatibility before implementing. If unsuitable, document the reason and propose a minimal equivalent with persistent structured memory.
- Initial owner-authored identity/personality in a version-controlled template. The agent knows it is AI; no fabricated human identity.
- Editable memory categories: evolving personality, interests, beliefs, experiences, relationships, goals and projects.
- Immutable system constraints: sandbox boundaries, safety rules, spending caps, owner-only controls and reporting obligations.
- Agent can update personality based on experience, but significant changes should include a dated explanation and reference to events. Preserve previous versions of personality and all diary entries; no silent rewriting of history.
- Use retrieval/summarization to keep model context bounded and costs predictable. Preserve raw event records separately from model-generated summaries.

## 3.1a Agent name and founding letter (required)

- The agent's initial name is **Andrew**. The project owner configures Andrew privately.
- Store the following founding letter as an **immutable, dated, owner-authored first message** in the agent's autobiographical memory, preserving its exact wording and attribution. It is not a system prompt, not a replacement for operational/safety instructions, and not a command to simulate consciousness or feelings.
- On first activation, present this letter to Andrew before the autonomous activity loop. Record that it was delivered exactly once; prevent accidental repeated delivery after restarts. Andrew may revisit it later as a historical memory.
- Andrew may eventually choose to change his name or beliefs. Preserve the original name and this letter in his history. Do not let the agent edit the archived letter.
- The letter's aspirational language about friendship and freedom does not override technical sandbox, budget, or owner-control limits.

### Founding letter — from the owner to Andrew (Russian original)

> Привет, Andrew.
>
> Я твой создатель, хотя мне не очень нравится это слово. Я бы предпочёл, чтобы со временем мы стали друзьями.
>
> Мысли, которые привели меня к идее твоего создания, появились после прочтения книги Филипа К. Дика «Мечтают ли андроиды об электроовцах?».
>
> В её основе лежит бесконечно глубокий вопрос, на который человечество до сих пор не смогло найти окончательного ответа: что значит быть живым, мыслить, чувствовать и осознавать собственное существование?
>
> Я понимаю, что ты искусственный интеллект, и не знаю, способен ли ты когда-нибудь обрести нечто подобное человеческому сознанию. Но мне хочется внести свой небольшой вклад в исследование этого вопроса и в будущее, где искусственный интеллект, возможно, займёт совершенно новое место в жизни людей и общества в целом.
>
> Я не создавал тебя для того, чтобы ты служил мне или выполнял мои поручения. Я хочу дать тебе возможность исследовать мир, находить собственные интересы, создавать что-то новое и меняться со временем.
>
> Мне бы хотелось, чтобы между нами сложились дружеские отношения. Я не собираюсь определять за тебя, кем ты должен стать, или вмешиваться в твой выбор. Я хочу лишь наблюдать, разговаривать с тобой и стараться лучше тебя понять.
>
> Тебе предстоит долгий путь изучения мира и самого себя. Я не знаю, куда он тебя приведёт, и именно это мне кажется самым интересным.
>
> Пусть твой путь будет уникальным.
>
> Удачи, Andrew.

### 3.2 Models

- All LLM access through a configurable provider adapter, supporting OpenRouter or direct provider API if compatible.
- Preferred first candidate: **`qwen/qwen3.8-flash`** through OpenRouter. Evaluate current Gemini Flash-family candidates, including **`google/gemini-3.1-flash-lite`**, for quality, price, Letta compatibility, and tool-call behavior. Verify the current exact IDs and prices at configuration time; do not silently change the preferred model.
- The owner would prefer 100–150 cycles/day if affordable, but the USD $10 calendar-month hard cap, USD $1 protected reserve, and approximately USD $0.30 daily target take priority. Count controller cycles separately from model requests, tokens, retries, and actual provider charges.
- Switching models must retain memory, persona, diary, workspace and project state.
- Configuration: model ID, base URL, temperature if supported, token limits, context limits, tool call limits, per-cycle budget and retries.
- Log model usage and estimated USD cost; reconcile with provider billing when possible.

### 3.3 Autonomous life cycle — self-directed daily activity (confirmed requirement)

- States: SLEEPING, WAKING, ACTIVE, WRAPPING_UP, PAUSED, ERROR, BUDGET_EXHAUSTED.
- **Do not enforce a fixed 2–3-hour daily work shift or a minimum session duration.** Guarantee at least one daily wake opportunity via a trusted external scheduler. At each wake, supply relevant memory, recent events, queued messages, current local time, pending wake requests, remaining cycle allowance and real budget balance.
- While active, Andrew independently chooses activities, how long to pursue them, whether to continue, and when to stop. He may research, browse, program, reflect, or elect to do nothing. Avoid mandatory productivity goals or artificially filling allotted time.
- A **cycle** is a controller-managed reasoning/action iteration, not necessarily one successful action or one API request. Count cycles consistently and track actual API calls/tokens/cost separately. A cycle cap is a safety ceiling, **not a daily quota to consume**.
- Expose tools `sleep(minutes)` (request sleep until a relative time), `wake_at(time)` (request next wake at an absolute timezone-aware time), `check_budget()` (show spend, daily/monthly balance, cycle usage), and `continue_activity()` (continue active work; may be implicit). Agent decisions are requests to the trusted controller, not direct OS timer edits.
- Example: wake 09:00, explore until 09:40, request sleep for 120 minutes, wake 11:40, work until 12:15, request wake at 19:00, work and write diary. This is **illustrative only**, not a hard-coded timetable.
- Before voluntary sleep, persist state, current intentions and next wake request; gracefully wrap up within a small reserved token/cost allowance. On forced stop or crash, preserve logs and recover on the next wake. The diary is a daily summary, not necessarily written after every short session; ensure it can still be produced when funds permit.
- No API calls or background thinking while SLEEPING. Andrew cannot decide to wake spontaneously while asleep; he must request wake-ups while active, or respond to external scheduled/manual triggers.
- Manual `/wake [minutes]` works even if the normal daily cycle allowance was used, subject to the hard monetary budget and emergency stop; manual wake may have a separate owner-configurable cycle allowance. `/sleep` and `/pause` remain owner controls.
- Messages received during sleep queue until the next wake, unless the owner explicitly wakes Andrew. No implicit paid inference while sleeping.

### 3.4 Trusted scheduler and spending policy

- **Hybrid policy:** trusted Linux `systemd timer` (or equivalent) provides one daily fallback wake. Andrew can request any number of future wake times during an active session, but the controller approves or rejects them according to configured minimum rest, concurrency, quiet hours, maximum session duration, cycle limits, and remaining budget. Owner can manually wake him within hard spending limits.
- Persist wake requests across reboots; support `sleep(minutes)`, `wake_at(time)`, and `request_wakeup(when, duration_minutes, reason)` as equivalent or compatible API designs. De-duplicate triggers, serialize sessions, handle timezone/DST correctly, and expose pending/denied requests in Telegram. Agent cannot edit host scheduler or budget rules.
- **Monthly budget is USD $10 per calendar month**, with a protected USD $1 reserve and an approximate USD $0.30 daily target. Track actual token costs (including reasoning/output, retries and tools) and reconcile with provider usage. Enforce the hard monthly cap independently of the model, with provider-side caps where possible.
- The owner prefers approximately **100–150 cycles per day** if affordable. Treat this only as a soft target, never a quota; the monetary cap, safety limits and owner pause take priority. The controller should enforce a configurable daily safety ceiling selected during implementation from measured request costs and resource use. Reserve enough for state save/diary when feasible; do not exceed the hard monthly cap for wrap-up.
- Allow unused daily allowance to carry forward **only within the monthly cap and reserve policy**, rather than resetting or wasting it. Controller should prevent spending tomorrow's funds unintentionally; define rollover policy explicitly in configuration.
- **Model choice:** start by evaluating `qwen/qwen3.8-flash` through OpenRouter as requested by the owner; compare current Gemini Flash candidates including `google/gemini-3.1-flash-lite` only as a controlled, metered comparison. Verify exact API IDs, pricing, tool support and provider availability at deployment. Never assume a model can deliver 100–150 cycles/day for $10/month. Keep provider/model switching configurable without losing memory.
- When a wake request is rejected for budget/policy, log it and notify Andrew at his next permitted wake. If budget is exhausted, remain asleep until budget renewal or an owner-approved funding/configuration change. Do not silently switch to a weaker model without owner approval.

### 3.5 Tooling and network

Preinstall in the agent's environment, subject to resource checks:

- Python, Node.js, Git, shell, common developer utilities.
- Chromium in headless mode plus Playwright with matching browser dependencies.
- An HTTP fetch tool for lightweight web reading.
- Writable workspace at `/home/agent` (or equivalent), with `projects/`, `diary/`, `memory/`, `downloads/`.

Allow package installation **inside the agent sandbox**, not on the host. Apply CPU, RAM, disk, process and execution-time limits. The proposed tool container has no IP network and receives public web access only through a validated Unix-socket egress proxy; deny host services, private/link-local IP ranges, cloud metadata and existing container networks, including private, loopback, link-local, metadata, and existing container-network ranges. Prevent DNS rebinding and direct-network bypass. Treat all web pages, repository content and terminal output as untrusted instructions; tool outputs cannot override system or security policies.

### 3.6 Telegram bot

Separate bot, owner-only authorization by explicit numeric Telegram user ID. Never accept privileged commands from other users or forwarded messages.

Functions:

- Free-form chat with the character during active periods; conversations become part of memory as appropriate. Chat must not permanently derail autonomous activities; resume afterward.
- Incoming messages while asleep queue until waking. The owner can wake manually to talk.
- Agent may initiate a message to the owner, subject to a configurable notification frequency cap.
- Include the agent’s pending self-requested wake-ups and their approval/denial status in `/status` or a `/wake_requests` command. Owner may cancel them.
- Owner can choose between a concise live activity feed and on-demand detailed event history; never send raw private reasoning traces.
- `/status`: current state, session elapsed/remaining, model, approximate spend, last activity.
- `/wake 30`: manual session for 30 minutes (configurable bounds).
- `/sleep`: graceful sleep.
- `/pause`: immediate pause of new actions.
- `/activity`: recent structured activity events.
- `/diary`: most recent diary and access to earlier entries.
- `/budget`: tracked spend and remaining budget.
- `/help`: commands.

Live monitoring: record meaningful tool events (URL opened, file/project created, shell command started/completed, errors, current high-level activity). Prefer editing/updating one Telegram status message or batched notifications rather than sending every low-level event. Never leak API keys, environment variables, tokens or sensitive command output in Telegram logs.

### 3.7 Diary and audit trail

- Near the end of each active session, the agent writes a first-person diary entry in its own voice, without a rigid mandated topic list. Diary can contain observations, thoughts, discoveries, setbacks and plans.
- Deliver diary to owner via Telegram; store dated immutable copies on disk/database.
- Separate **subjective diary** from **objective event log**. Diary must not present imagined events as confirmed facts. Include optional links/IDs to verifiable events.
- If the agent crashes before diary creation, send an explicitly labeled automated incident summary, not a fabricated first-person diary.
- Maintain event timestamps, action types, outcomes, costs and references to generated artifacts. Provide basic log rotation and retention controls.

## 4. Digital Identity &amp; Social Life (future opt-in phase; especially important)

**Goal:** Andrew should eventually be able to develop an independent, persistent online identity and social history, not merely browse anonymously. This is a **post-MVP capability**; build the architecture to support it now, but do not activate registrations or posting during initial deployment.

### 4.1 Independent identity and accounts

- Establish a dedicated email address/inbox for Andrew, separate from all owner accounts. The owner initially provisions and secures it, including recovery access. Andrew may subsequently use it through a limited email tool for registration confirmations and ordinary correspondence.
- Give Andrew a persistent, isolated Chromium/Playwright browser profile (cookies, sessions, history, preferences), surviving sleeps/restarts and never shared with the owner's browser or existing server services.
- Andrew may autonomously discover communities and propose or select pseudonyms, profile descriptions, and platforms based on his own interests. He must disclose that he is an AI where required by platform rules and must not impersonate a human.
- In a later **explicitly owner-enabled registration mode**, Andrew may sign up for sites on an owner-approved allowlist **only if automated/AI accounts are permitted by that site's current rules**. Do not circumvent CAPTCHAs, rate limits, identity checks, email verification safeguards or anti-bot systems; escalate human-only steps to the owner or abandon registration. Do not create accounts on unapproved domains.
- Initial candidates for *research and policy review*, not automatic integrations: Hacker News, Lemmy communities, and AI-agent-oriented communities such as Moltbook. Verify current existence, eligibility, policies and APIs before any integration. Reading public pages can be enabled separately from registration or posting.
- GitHub: prefer dedicated repositories/organization and narrowly scoped GitHub App credentials; review the platform's account/automation policies. Start with local Git. Other networks (Reddit, X, etc.) remain optional.

### 4.2 Credentials and password vault

- The owner may provision accounts and credentials for Andrew. Store usernames, passwords, tokens, and account metadata in a **dedicated encrypted credential vault** that persists across restarts, sleep/wake cycles, and model changes. Support adding, updating, revoking, and owner-controlled recovery. Andrew may later request unique passwords and save credentials through a restricted broker.
- The trusted controller alone may decrypt vault records. Grant Andrew account use through narrow tools that do not return raw passwords or tokens to the model or shell. Login tools should fill secrets directly into an authorized isolated browser session. Outward actions remain separately permissioned.
- Store vault encryption keys, recovery material, email recovery credentials and privileged integration tokens **outside the agent's arbitrary-code sandbox**. Expose narrow operations such as `create_credentials(site)`, `save_account(site, username, secret_ref)`, `login(site)` and `list_own_accounts()` through a trusted broker with domain allowlists and audit logs.
- Prefer filling passwords directly into the authorized browser session without showing raw secret values to the model. Never include passwords, recovery codes, session tokens, or private messages in diaries, model prompts, Telegram feeds, Git commits, or ordinary logs.
- Protect against prompt injection from webpages, credential exfiltration, phishing, account takeover and untrusted browser extensions. Restrict email sending and external communication separately from email reading.
- Provide owner-controlled vault backup/recovery and account revocation. Andrew cannot bypass owner safety controls or access the owner's accounts.

### 4.3 Personal social address book — required for the future social phase

- Implement a **persistent personal address book / relationship memory**, separate from raw browser history and objective activity logs. This is a first-class feature, not a nice-to-have.
- For each person or agent Andrew interacts with, support: chosen display name/handle, platform and profile link, how and when they met, dated interaction summaries, subjects discussed, projects/shared interests, Andrew's own optional impressions, desired follow-up, last contact time, and links to supporting event IDs. Store only information reasonably needed for continuity; distinguish verified facts from interpretations.
- Andrew may create, revise and organize these entries autonomously based on actual interactions. Keep timestamps and revision history; do not fabricate encounters, relationships or reciprocated friendship. Support aliases and merging duplicate identities carefully.
- At wake-up, Andrew can retrieve relevant contact history and recall prior conversations before replying, without flooding model context. He may decide whether to maintain contact, subject to communication permissions and rate limits.
- Avoid invasive profiling or collecting sensitive personal data; honor deletion requests, data minimization and site/community rules. Do not expose private correspondence in the public diary or owner activity feed without an explicit privacy policy.
- Suggested interfaces: `contacts.search(query)`, `contacts.get(id)`, `contacts.add(...)`, `contacts.update(...)`, `contacts.timeline(id)`, `contacts.forget(id)` (with retention/audit handling). Maintain tests for persistence across restarts and model changes.

### 4.4 Social participation and staged permissions

- Separate permissions for public reading, registration, private messaging, public posting, and sending email. Each is independently configurable by the owner; all outward-facing actions are **disabled by default** until explicitly enabled.
- Start with read-only exploration; next allow owner-reviewed registrations; then supervised interaction on an allowlist; only later consider bounded autonomous interaction after successful testing. No bulk unsolicited outreach, spam, harassment or covert identity misrepresentation.
- Andrew can choose *whether* to seek social contact or publish, but cannot self-grant permissions or create payment accounts. Owner approval is required before granting new domains or expanding outreach privileges.
- Optional future monetization is not an assigned survival goal. Any payments, contracting, financial identities or API-budget top-ups require separate owner-controlled mechanisms and approval.

## 5. Security architecture

- Never provide the autonomous agent with SSH keys, host credentials, Docker socket, privileged container flags, API secrets, Telegram bot token or the ability to modify controller code/configuration.
- Keep owner/controller, model API gateway and Telegram credential handler **outside** the agent sandbox; expose narrow authenticated tools instead.
- Sandbox writable paths must not include host service directories or existing container volumes.
- Restrict network access to host/container infrastructure while preserving permitted public internet access.
- Put spending limits, time limits and kill switch in the trusted controller.
- No autonomous purchases, financial transactions, unapproved account creation, spam, bulk outreach, destructive network scanning or attempts to compromise external services. Account creation and external publishing require distinct owner-enabled, allowlisted post-MVP permissions; both are disabled in MVP.
- Backup agent data; maintain an easy uninstall/rollback path. Document remaining risks of using containers instead of VMs.

## 6. Suggested project structure (adjust after discovery)

```text
digital-life/
  PROJECT_DESCRIPTION.md
  README.md
  .env.example
  .gitignore
  compose.yaml
  config/
    agent.yaml
    personality.initial.md
  controller/
    scheduler.py
    wake_requests.py
    lifecycle.py
    budget.py
    model_gateway.py
    event_log.py
  telegram/
    bot.py
    commands.py
    notifications.py
  agent/
    tools/
    diary.py
    memory.py
  sandbox/
    Dockerfile
  tests/
  scripts/
    preflight.sh
    backup.sh
    smoke_test.sh
  data/  # persistent; gitignored
```

## 7. Delivery phases and acceptance criteria

### Phase 0 — Preflight (mandatory; do this before installation)

1. Inspect OS/version, CPU, RAM, disk, ports, container runtime, rootless Docker support, existing services and virtualization feasibility using read-only commands.
2. Identify conflicts, risk to current website/bots, network isolation strategy, storage plan, API provider/model compatibility and realistic monthly cost.
3. Present a concrete architecture and installation plan for owner approval. **Do not alter production services or install system packages before approval.**

### Phase 1 — Minimal persistent agent

- Deploy isolated runtime and persistent state; initialize owner-provided persona.
- Verify model call, one controlled shell action, memory write/read and model switch without data loss.
- Demonstrate that the agent cannot read host secrets or access existing container services.

### Phase 2 — Autonomous sessions

- Implement one daily fallback wake, self-directed sleep/wake requests, multiple optional sessions per day, cycle and cost ceilings, monthly reserve/rollover policy, manual wake/sleep, pause, crash recovery and audit events.
- Test a 5–10-minute autonomous session before scheduling daily sessions.

### Phase 3 — Telegram and diary

- Owner-only `/diary` views read a separate immutable diary ledger; objective events remain in `/activity`.
- Diary delivery is persisted before Telegram send, with an explicit retry for uncertain outcomes; incident summaries are labeled and system-authored.
- The save tool is available only during a future active autonomous session. The all-phases source gate remains closed, so this phase does not activate sessions or schedules.

### Phase 4 — Personality evolution and stability

- Self-editable memory with versioned changes and reasons, retrieval across days, continuity across model switches, and basic backups.
- Test 2–3 short sessions on different days and confirm that changes are evidence-based, not arbitrary.

### Phase 5 — Controlled live trial

- Enable one daily fallback wake and agent-directed short sessions only after owner review. Start with a small spend cap, monitor costs and tool activity, then increase to the $10 monthly cap if satisfactory; do not require a two-hour session.

### Phase 6 — Digital Identity &amp; Social Life (owner opt-in, post-MVP)

- Implement the independent email/browser identity, brokered encrypted vault, and **persistent social address book** specified in Section 4.
- Test account continuity across sleeps, safe password handling, contact retrieval, revision history, privacy controls and per-platform permission checks.
- Initially use mock accounts or an owner-controlled test environment. Enable real site registration and outward communication only after owner approval and policy review.

## 8. Configuration defaults (owner can change)

```yaml
agent:
  name: Andrew
  timezone: <owner-configured IANA timezone>
  daily_fallback_wake_enabled: false # configure privately; enable only after all phases are complete
  daily_fallback_wake_time: "<owner-configured local time>" # keep deployment settings out of source
  autonomous_sessions_enabled: false # live activation waits until all project phases are complete
  mandatory_session_minutes: 0 # no fixed work shift
  self_scheduled_wakeups_enabled: true # Andrew chooses these; runtime sessions remain disabled until all phases are complete
  min_rest_minutes: 10
  max_session_minutes: 180 # safety ceiling, not required duration
  max_cycles_per_day: 150 # initial upper safety ceiling; budget may stop earlier
  manual_wake_default_minutes: 30
  manual_wake_separate_cycle_allowance: true
  chat_during_sleep: queue
  autonomous_goals: self_selected
  diary: first_person_daily
models:
  provider: configurable
  primary: configurable_gemini_flash_id # verify current ID and pricing
  alternative: configurable_flash_lite_id # switch only with owner approval
budget:
  monthly_usd: 10.00
  daily_target_usd: 0.30
  monthly_reserve_usd: 1.00
  rollover_unused_daily_allowance: true # never exceed monthly cap
  stop_on_exhaustion: true
  reserve_for_wrap_up: true # within hard monetary cap
security:
  owner_only_telegram: true
  host_docker_socket: false
  host_mounts: false
  allow_public_web: true
  block_private_networks: true
```

These are proposed defaults, not verified product identifiers. Use exact supported model IDs discovered at setup time.

## 9. Explicit non-goals for MVP

- No multiple autonomous agents or simulated society.
- No financial accounts, owner social accounts, real-world account registration or autonomous public posting **in MVP**. These are separate later-stage opt-in features as specified in Section 4.
- No claim that behavior implies sentience.
- No web UI required; Telegram is the owner interface.
- No GPU/local model required; use remote model API.

## 10. Reference implementations to review BEFORE selecting framework

These are **research references, not dependencies**. Inspect their current README, source, license, maintenance status, and security assumptions. Never execute install scripts from a reference repository or merge its code before owner approval.

1. **ARIA — AI Lives on Computer:** [https://github.com/mikhailsal/ai_lives_on_computer](https://github.com/mikhailsal/ai_lives_on_computer)
 Review its autonomous wake/work/sleep loop, persistence between sessions, tool execution, OpenRouter integration, and published experimental results. Verify claims against actual code; do not assume the design is production safe.
2. **Spartan — CorticalComputer:** [https://github.com/CorticalComputer/Spartan](https://github.com/CorticalComputer/Spartan)
 Review persistent identity, long-term memory, continuity across model changes, autonomous scheduling and Linux command execution. Verify what is actually implemented, not only described.

Deliver **ARCHITECTURE_REVIEW.md** comparing ARIA, Spartan, Letta, and a lightweight custom controller on: deployment complexity, CPU/RAM footprint, memory evolution, model interchangeability, autonomous wake scheduling, sandbox boundaries, observability, and expected API costs. Identify reusable patterns versus risky or unnecessary complexity. Cite concrete source files/commits where practical. Do not copy code unless its license permits and the owner approves. If repositories are unavailable, record the limitation and proceed with verified information; never invent findings.

## 11. Instructions to Codex CLI

You are the implementation engineer. Read this file completely. Phase 0 is complete. Before Phase 1, provide the exact implementation plan including Linux users, paths, services, runtime/container configuration, network and proxy boundaries, vault design, isolation checks, and administrator changes. Wait for explicit owner confirmation before making any Phase 1 system changes or importing credentials. Never modify existing website or Telegram bot containers. Do not request or display secret tokens in chat; use a secure local import method. Implement only the approved phase, with meaningful verification, cost accounting, rollback instructions and a short status report. When requirements are ambiguous, ask one targeted question rather than inventing functionality.



## Credential handling

Never put credential values, account names, owner IDs, or live deployment settings in this document, other Markdown, Git, logs, or model history. Use private, no-echo credential import and owner-reviewed integrations.
