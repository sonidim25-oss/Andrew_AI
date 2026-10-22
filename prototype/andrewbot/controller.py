from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import logging
from .latency import record_failure
from .budget import budget_snapshot
from .configuration import OUTPUT_TOKEN_COST_ESTIMATE, ModelConfig, load_model_config
from .diary import format_diary_entry, parse_diary_command, validate_diary_content
from .telegram_menu import normalize_owner_text, HELP_TEXT
from .appserver import AppServerClient, LettaTurnError, LettaTurnCancelled
from .tool_errors import ToolRejected
from .memory import ResearchJournal, ResearchValidationError, SessionHistory, validate_memory_text, note_untrusted_memory_source
from .gmail_tools import GMAIL_TOOL_NAMES, GMAIL_ACTION_CONTEXT
from .media import MEDIA_TOOL_NAMES, MEDIA_AUTHORIZATION
from .skills import SkillCatalog, SKILL_TOOL_NAMES
import json
from .runner_client import TOOL_CANCELLATION


class Controller:
    def __init__(self, store, model, owner_id, monthly_limit_usd=10.0,
                 monthly_reserve_usd=1.0, daily_target_usd=0.30,
                 timezone_name="America/Toronto", fallback_wake_time="09:00",
                 autonomous_enabled=False, fallback_enabled=False, memory_service=None, model_config=None, credit_reader=None,
                 tool_runtime=None, visit_history=None, gmail_tools=None, conversation_idle_seconds=300, health_snapshot=None, media=None, skill_catalog=None,
                 daily_spend_alert_usd=None):
        self.store = store
        self.media = media
        self.skill_catalog = skill_catalog if skill_catalog is not None else SkillCatalog()
        self.health_snapshot = health_snapshot
        self.model = model
        self.owner_id = int(owner_id)
        self.monthly_limit_usd = float(monthly_limit_usd)
        self.monthly_reserve_usd = float(monthly_reserve_usd)
        self.daily_target_usd = float(daily_target_usd)
        # Notification only: spending stays agent-managed with no controller cap.
        self.daily_spend_alert_usd = float(daily_spend_alert_usd) if daily_spend_alert_usd else None
        self.credit_reader = credit_reader
        self.timezone_name = timezone_name
        self.fallback_wake_time = fallback_wake_time
        self.autonomous_enabled = bool(autonomous_enabled)
        self.fallback_enabled = bool(fallback_enabled) and self.autonomous_enabled
        self.memory_service = memory_service
        self.tool_runtime = tool_runtime
        self.runner_recovery = None
        self.visit_history = visit_history
        self.gmail_tools = gmail_tools
        self.conversation_idle_seconds = max(0, int(conversation_idle_seconds))
        if model_config is not None and not isinstance(model_config, ModelConfig):
            raise ValueError("model configuration is invalid")
        self.model_config = model_config or load_model_config({})

    def check_spend_alert(self, now=None):
        if self.daily_spend_alert_usd is None:
            return False
        snapshot = budget_snapshot(self.store, now=now)
        if snapshot['daily_cost'] < self.daily_spend_alert_usd:
            return False
        text = (f"Spending alert: ${snapshot['daily_cost']:.2f} used today ({snapshot['day']}), "
                f"above the ${self.daily_spend_alert_usd:.2f} alert level. "
                f"This month so far: ${snapshot['monthly_cost']:.2f}. "
                "Nothing was stopped; send /pause to pause Andrew.")
        if not self.store.queue_daily_spend_alert(snapshot['day'], self.owner_id, text):
            return False
        self.store.add_activity(f"Daily spending alert sent ({snapshot['day']})")
        return True

    @property
    def research(self):
        return ResearchJournal(self.store)

    def _periods(self, now=None):
        zone = ZoneInfo(self.timezone_name)
        current = datetime.now(timezone.utc) if now is None else now
        if current.tzinfo is None:
            raise ValueError("budget time must be timezone-aware")
        local = current.astimezone(zone)
        return local.strftime("%Y-%m"), local.strftime("%Y-%m-%d")

    @staticmethod
    def _local_wake_candidates(day, hour, minute, zone):
        naive = datetime(day.year, day.month, day.day, hour, minute)
        # A spring-forward gap is shifted to the first valid wall-clock minute.
        for _ in range(181):
            candidates = []
            for fold in (0, 1):
                candidate = naive.replace(tzinfo=zone, fold=fold)
                round_trip = candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
                if round_trip == naive and all(
                    item.astimezone(timezone.utc) != candidate.astimezone(timezone.utc)
                    for item in candidates
                ):
                    candidates.append(candidate)
            if candidates:
                return candidates
            naive += timedelta(minutes=1)
        raise ValueError("no valid local wake time near the requested time")

    @classmethod
    def _next_local_wake(cls, now_local, hour, minute, zone):
        now_utc = now_local.astimezone(timezone.utc)
        for offset in (0, 1):
            day = now_local.date() + timedelta(days=offset)
            eligible = [item for item in cls._local_wake_candidates(day, hour, minute, zone)
                        if item.astimezone(timezone.utc) > now_utc]
            if eligible:
                return min(eligible, key=lambda item: item.astimezone(timezone.utc))
        raise ValueError("no future local wake time could be resolved")

    def budget_allows(self, estimated_cost_usd):
        if self.store.get_state('restore_recovery_required', '0') == '1':
            return False, 'restore_reconciliation'
        return True, "ok"

    def handle_update(self, update, allow_time_entry=True):
        from .telegram_menu import is_model_input
        message = update.get("message") or {}
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        if any(key in message for key in ("forward_origin", "forward_from", "forward_from_chat", "forward_sender_name")):
            return None
        text = message.get("text")
        if sender.get("id") != self.owner_id or chat.get("id") != self.owner_id or chat.get("type") != "private":
            return None
        if not isinstance(text, str) or not text.strip():
            return None
        text = normalize_owner_text(text)
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        media_reply_kind = {'/voice_reply': 'voice', '/image_reply': 'image'}.get(command)
        if media_reply_kind:
            if self.media is None or not self.media.policy.enabled:
                return 'Media generation is disabled.'
            if media_reply_kind == 'voice' and not self.media.policy.voice_enabled:
                return 'Voice generation is disabled.'
            if len(text.split(maxsplit=1)) != 2:
                return 'Add your question after ' + command
        if not is_model_input(text):
            self.store.set_state("telegram_wake_prompt_until", "")
            return self._command(command, text, update_id=update.get("update_id"))
        prompt_until = self.store.get_state("telegram_wake_prompt_until", "")
        if prompt_until and allow_time_entry:
            try:
                pending = datetime.now(timezone.utc) < datetime.fromisoformat(prompt_until)
            except (ValueError, TypeError):
                pending = False
            if pending:
                # Restrict input to a time so text cannot become another command.
                parts = text.split(":")
                if (len(parts) != 2 or not all(part.isascii() and part.isdigit() for part in parts)
                        or len(parts[0]) not in (1, 2) or len(parts[1]) != 2
                        or not 0 <= int(parts[0]) <= 23 or not 0 <= int(parts[1]) <= 59):
                    return "Enter a 24-hour time such as 09:00, or send /cancel to return to chat."
                self.store.set_state("telegram_wake_prompt_until", "")
                return self._command("/wake_at", "/wake_at " + text)
            self.store.set_state("telegram_wake_prompt_until", "")
            return "Time entry expired. Tap ⏰ Set wake time again, or resend your message to chat."
        sleeping = self.store.get_state("sleeping", "0") == "1"
        paused = self.store.get_state("paused", "0") == "1"
        if sleeping or paused:
            self.store.add_message(self.owner_id, "user", text, queued=True)
            self.store.add_activity("Message queued while Andrew is sleeping or paused")
            return "I’m asleep or paused. Your message is saved for when I wake."
        message_id = self.store.add_message(self.owner_id, "user", text, queued=True)
        if self.store.get_state('autonomy_error', ''):
            self.store.add_activity('Owner message queued while automatic model calls are stopped')
            return 'Your message is saved and has not been sent to the model. Automatic model calls are stopped after an error. Use /wake to resume.'
        recovery = getattr(self, 'local_recovery', None)
        if recovery is not None and recovery.enabled is True and recovery.has_pending():
            # Held journaled work hands over to waiting owner input before any new
            # turn may start; the queued message is answered after that handoff.
            self.store.add_activity('Owner message queued behind held journaled work')
            return 'Your message is saved. I’m first closing an earlier unfinished step without repeating it; then I’ll answer.'
        owner_revision = self.store.get_state('owner_control_revision', '0')
        self.note_conversation(owner_revision)
        messages = self._bounded_messages([
            {"role": row["role"], "content": row["content"]}
            for row in self.store.recent_messages(self.owner_id, limit=11)
        ] + [{"role": "user", "content": text}])
        max_cost = self._estimate_cost(messages)
        allowed, reason = self.budget_allows(max_cost)
        if not allowed:
            self.store.add_activity(f"Model request blocked by {reason}")
            return "Your message is queued until the configured budget allows model requests."
        if not self.store.claim_owner_message(self.owner_id, message_id):
            return "Your message is saved for delivery."
        try:
            if media_reply_kind:
                messages[-1]['content'] += (f'\nThe owner explicitly authorizes one {media_reply_kind} response in this turn. '
                                            f'Use {media_reply_kind}.generate with owner_request_id="owner-reply:{message_id}". '
                                            'Do not claim delivery before the media job is acknowledged.')
            reply, usage = self._reply_with_media(messages, message_id, media_reply_kind)
            month, day = self._periods()
            actual = self._actual_cost(usage)
            if not usage.get("accounted_by_gateway"):
                self.store.record_usage(month, day, max(actual, max_cost if not usage else 0.0),
                                        int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
            self.store.add_message(self.owner_id, "assistant", reply)
            self.store.add_activity("Andrew replied to the owner")
            self.store.release_queued(self.owner_id, [message_id])
            self.note_conversation(owner_revision, completed=True)
            return reply
        except Exception as error:
            record_failure(error, 'cancelled' if isinstance(error, LettaTurnCancelled) else None)
            self.store.hold_uncertain_queued_claim([message_id])
            self.store.add_activity("Owner message outcome uncertain; held to avoid duplicate delivery")
            return self.failed_turn(error, owner_revision)

    def note_conversation(self, owner_revision, completed=False, now=None):
        current = datetime.now(timezone.utc) if now is None else now
        return self.store.note_conversation(
            (current + timedelta(seconds=self.conversation_idle_seconds)).isoformat(),
            self.autonomous_enabled, owner_revision, completed,
        )

    def failed_turn(self, error, owner_revision):
        interrupted = ('The turn was interrupted by an owner command. Your message is saved with uncertain '
                       'delivery and will not be repeated automatically. Use /replay to retry.')
        if (isinstance(error, LettaTurnCancelled)
                or self.store.get_state('owner_control_revision', '0') != owner_revision):
            return interrupted
        category = (error.safe_category if isinstance(error, LettaTurnError) else
                    'timeout' if isinstance(error, TimeoutError) else 'turn_failed')
        if not self.store.record_session_failure(category, self.owner_id, owner_revision=owner_revision, notify=False):
            return interrupted
        return self.store.failure_notice(category)

    def _estimate_cost(self, messages):
        # Provisional accounting estimate; neither term enforces an output cap.
        input_bytes = sum(len(item["content"].encode("utf-8")) for item in messages)
        input_tokens = min(48000, max(1, input_bytes))
        return (input_tokens * self.model_config.input_usd_per_million
                + OUTPUT_TOKEN_COST_ESTIMATE * self.model_config.output_usd_per_million) / 1_000_000

    @staticmethod
    def _bounded_messages(messages):
        bounded = []
        used = 0
        for item in reversed(messages):
            encoded = item["content"].encode("utf-8")
            remaining = 48_000 - used
            if remaining <= 0:
                break
            if len(encoded) > remaining:
                encoded = encoded[-remaining:]
                item = {**item, "content": encoded.decode("utf-8", errors="ignore")}
            else:
                item = dict(item)
            bounded.append(item)
            used += len(item["content"].encode("utf-8"))
        return list(reversed(bounded))

    def _actual_cost(self, usage):
        if usage.get("cost") is not None:
            return max(0.0, float(usage["cost"]))
        return (int(usage.get("prompt_tokens", 0)) * self.model_config.input_usd_per_million
                + int(usage.get("completion_tokens", 0)) * self.model_config.output_usd_per_million) / 1_000_000

    def check_scheduled_wake(self, now=None):
        if self.store.get_state('autonomy_error', ''):
            return False
        if self.store.get_state("paused", "0") == "1":
            return False
        if self.store.get_state("sleeping", "0") != "1":
            return False
        wake_at = self.store.get_state("sleep_until", "")
        if not wake_at:
            return False
        current = datetime.fromisoformat(now) if isinstance(now, str) else now
        if current is None:
            current = datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("wake comparison time must be timezone-aware")
        return self.store.scheduled_wake_state(current, self.owner_id)

    def check_fallback_wake(self, now=None):
        if self.store.get_state("paused", "0") == "1":
            return False
        try:
            zone = ZoneInfo(self.timezone_name)
            hour_text, minute_text = self.fallback_wake_time.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError
        except (ZoneInfoNotFoundError, ValueError, AttributeError):
            raise ValueError("fallback wake timezone or time is invalid") from None
        current = datetime.fromisoformat(now) if isinstance(now, str) else now
        if current is None:
            current = datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("fallback wake comparison time must be timezone-aware")
        local_now = current.astimezone(zone)
        local_day = local_now.date().isoformat()
        if (local_now.hour, local_now.minute) < (hour, minute):
            return False
        return self.store.process_fallback_wake(local_day, self.owner_id)

    def flush_queued(self):
        if (self.store.get_state("sleeping", "0") == "1" or self.store.get_state("paused", "0") == "1"
                or self.store.get_state('autonomy_error', '')):
            return None
        queued = self.store.claim_queued_messages(self.owner_id)
        if not queued:
            return None
        message = "Saved owner messages:\n" + "\n".join(row["content"] for row in queued)
        bounded = self._bounded_messages([{"role": "user", "content": message}])
        estimate = self._estimate_cost(bounded)
        allowed, reason = self.budget_allows(estimate)
        if not allowed:
            self.store.restore_queued_claim([row["id"] for row in queued])
            self.store.add_activity(f"Queued messages held by {reason} budget gate")
            return None
        owner_revision = self.store.get_state('owner_control_revision', '0')
        self.note_conversation(owner_revision)
        try:
            # Saved work runs before the autonomous cycle after a wake. Its
            # model/tool rounds need the same absence of a total turn deadline;
            # provider network waits and owner cancellation still apply.
            reply, usage = self.model.reply(bounded, timeout=None)
            month, day = self._periods()
            if not usage.get("accounted_by_gateway"):
                actual = self._actual_cost(usage)
                self.store.record_usage(month, day, max(actual, estimate if not usage else 0.0),
                                        int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
            self.store.add_message(self.owner_id, "assistant", reply)
            self.store.add_activity(f"Andrew replayed {len(queued)} queued message(s)")
            self.store.release_queued(self.owner_id, [row["id"] for row in queued])
            self.note_conversation(owner_revision, completed=True)
            return reply
        except Exception as error:
            record_failure(error, 'cancelled' if isinstance(error, LettaTurnCancelled) else None)
            self.store.hold_uncertain_queued_claim([row["id"] for row in queued])
            self.store.add_activity("Queued message replay outcome uncertain; messages held to avoid duplicate delivery")
            return self.failed_turn(error, owner_revision)

    def _reply_with_media(self, messages, message_id, kind):
        authorization = (self.media, kind, 'owner-reply:' + str(message_id)) if kind and self.media else None
        token = MEDIA_AUTHORIZATION.set(authorization)
        try:
            return self.model.reply(messages)
        finally:
            MEDIA_AUTHORIZATION.reset(token)

    def reconcile_actions(self):
        # Read-only network work runs separately from owner controls and models.
        if any(self.store.get_state(key, '0') == '1'
                for key in ('paused', 'sleeping', 'restore_recovery_required')):
            return
        self.store.reconcile_telegram_receipts(self.owner_id)
        if self.media is not None:
            self.media.reconcile_deliveries()
        if self.gmail_tools is not None:
            self.gmail_tools.reconcile_pending()

    def execute_tool(self, name, arguments, *, recovery=None):
        if recovery is not None:
            from .recovery_dispatch import RecoveryDispatch
            coordinator=getattr(self,'local_recovery',None)
            token=TOOL_CANCELLATION.set(coordinator._active_cancel if coordinator is not None else None)
            try:
                return RecoveryDispatch(self.store,self.runner_recovery).run_tool(
                    recovery, name, arguments, lambda: self.execute_tool(name, arguments))
            finally:TOOL_CANCELLATION.reset(token)
        if (name in {'python.run','node.run','shell.run'} and self.store.get_state('runner_receipt_store_id','')
                and self.store.get_state('model_turn_origin','')=='autonomous'):
            raise ValueError('Journaled runner admission is required')
        if name in (AppServerClient.EXTERNAL_TOOL_NAMES | {'gmail.read', 'research.list', 'research.get'}):
            note_untrusted_memory_source()
        known = (AppServerClient.SAFE_CONTROLLER_TOOL_NAMES | AppServerClient.EXTERNAL_TOOL_NAMES
                 | GMAIL_TOOL_NAMES | MEDIA_TOOL_NAMES)
        context = self.store.research_tool_context()
        attempt = self.store.begin_cycle_tool(context, name) if name in known else None
        action_context = GMAIL_ACTION_CONTEXT.set((attempt, context[0], context[1]))
        outcome = 'error'
        confirmed = False
        try:
            result = self._execute_tool(name, arguments)
            outcome = 'returned'
            confirmed = True
            if name in AppServerClient.EXTERNAL_TOOL_NAMES:
                # Runner/browser failures can be returned as data after partial
                # side effects. A successful model answer does not settle them.
                confirmed = False
                if isinstance(result, str):
                    confirmed = result.startswith('Status: ok\n')
                    if result.startswith('Status: error\n'):
                        confirmed = False
                    elif result.startswith('{'):
                        try:
                            response, _ = json.JSONDecoder().raw_decode(result)
                        except ValueError:
                            confirmed = False
                        else:
                            confirmed = isinstance(response, dict) and response.get('status') == 'ok'
            return result
        except ToolRejected:
            # Known non-execution must settle the legacy tool ledger as well
            # as the journaled receipt. Unknown exceptions remain uncertain.
            confirmed = True
            outcome = 'rejected'
            raise
        finally:
            GMAIL_ACTION_CONTEXT.reset(action_context)
            self.store.finish_cycle_tool(attempt, confirmed)
            if name in known:
                self.store.record_research_tool(name, outcome, context)

    def _execute_tool(self, name, arguments):
        if not isinstance(arguments, dict):
            if name in {"memory_add_experience", "memory_propose_personality_edit"} and self.memory_service:
                kind = "personality" if name == "memory_propose_personality_edit" else "experience"
                self.memory_service.reject_edit_attempt(kind)
            raise ToolRejected("tool arguments must be an object")
        read_tools = {"check_budget", "recent_activity", "memory_search", "session_search", "session_read", "gmail.status", "gmail.reconcile", "media.status", "media.get", "skills.list", "skills.load", 'research.list', 'research.get', 'research.observations'}
        if self.store.get_state("paused", "0") == "1" and name not in read_tools:
            raise ToolRejected("owner pause can only be cleared by the owner with /wake")
        if self.store.get_state("sleeping", "0") == "1" and name not in read_tools:
            raise ToolRejected("sleep can only end at a scheduled wake or an owner wake")
        if (name in {"sleep", "wake_at"} and self.store.get_state("active_session_id", "")
                and not self.store.diary_entry(datetime.now(ZoneInfo(self.timezone_name)).date().isoformat())):
            raise ToolRejected("save today's diary before requesting autonomous sleep")
        if name in SKILL_TOOL_NAMES:
            return json.dumps(self.skill_catalog.execute(name, arguments), ensure_ascii=False)
        if name in {'session_search', 'session_read'}:
            return json.dumps(SessionHistory(self.store, self.owner_id).execute(name, arguments), ensure_ascii=False)
        if name in {'media.status', 'media.get', 'voice.generate', 'image.generate', 'image.edit', 'profile.propose_avatar'}:
            if self.media is None:
                raise ToolRejected('Media unavailable')
            return json.dumps(self.media.execute(name, arguments), ensure_ascii=False)
        if name.startswith('research.'):
            try:
                result = self.research.execute(name.split('.', 1)[1], arguments)
            except ResearchValidationError as error:
                result = error.result()
            return json.dumps(result, ensure_ascii=False)
        if name in {"memory_search", "memory_add_experience", "memory_propose_personality_edit"}:
            if self.memory_service is None:
                raise ToolRejected("memory service is unavailable")
            allowed = ({'query', 'limit'} if name == 'memory_search' else
                       {'content', 'record_type', 'event_ids', 'diary_ids', 'history_ids'} if name == 'memory_add_experience' else
                       {'content', 'reason', 'event_ids', 'diary_ids', 'history_ids'})
            if set(arguments) - allowed:
                self.memory_service.reject_edit_attempt('personality' if name == 'memory_propose_personality_edit' else 'experience')
                raise ToolRejected('unsupported memory arguments; provenance is assigned by the controller')
            if name == "memory_search":
                rows = self.memory_service.search(arguments.get("query"), arguments.get("limit", 8))
                if not rows:
                    return "No relevant memory records were found."
                result = "Relevant memory records (read-only):\n" + "\n".join(
                    f"{row['record_type']} {row['record_id']} ({row['created_at']}; author={row['source']}; "
                    f"provenance={row['provenance']}; basis={row['provenance_basis']}): "
                    f"{row['content']} [events={row['event_ids']}; diary={row['diary_ids']}; history={row['history_ids']}]"
                    for row in rows
                )
                encoded = result.encode("utf-8")
                maximum_bytes = 16_000
                if len(encoded) > maximum_bytes:
                    marker = "\n[Memory results truncated to fit the context limit.]"
                    available = maximum_bytes - len(marker.encode("utf-8"))
                    result = encoded[:available].decode("utf-8", errors="ignore").rstrip() + marker
                return result
            if name == "memory_add_experience":
                record_id = self.memory_service.add_experience(
                    arguments.get("content"), arguments.get("record_type"),
                    arguments.get("event_ids", []), arguments.get("diary_ids", []),
                    history_ids=arguments.get('history_ids', []), owner_id=self.owner_id,
                )
                return f"Memory record saved ({record_id})."
            revision_id = self.memory_service.propose_personality_edit(
                arguments.get("content"), arguments.get("reason"),
                arguments.get("event_ids", []), arguments.get("diary_ids", []),
                history_ids=arguments.get('history_ids', []), owner_id=self.owner_id,
            )
            if self.store.get_memory_record(revision_id)['provenance'] == 'untrusted':
                return f'Personality proposal saved ({revision_id}; untrusted evidence); active personality unchanged.'
            return f"Personality revision saved ({revision_id}); previous revisions are preserved."
        if name == "sleep":
            stop_note = validate_memory_text(arguments['reason'], 500) if arguments.get('reason') else ''
            minutes = arguments.get("minutes")
            if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:
                raise ToolRejected("sleep duration must be an integer from 1 to 1440 minutes")
            wake_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
            notice = f"I’m choosing to sleep. I expect to wake at {wake_at} (in {minutes} minutes)."
            self.store.choose_sleep(
                {"sleeping": "1", "sleep_until": wake_at, "session_start_requested": ""},
                "Andrew requested timed sleep", self.owner_id, notice, datetime.now(timezone.utc), stop_note,
            )
            return f"Andrew is sleeping and will wake in {minutes} minutes."
        if name == "wake_at":
            stop_note = validate_memory_text(arguments['reason'], 500) if arguments.get('reason') else ''
            value = arguments.get("time")
            if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
                raise ToolRejected("wake time must use local HH:MM format")
            try:
                hour, minute = (int(piece) for piece in value.split(":"))
                if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                    raise ValueError
                zone = ZoneInfo(self.timezone_name)
            except (ValueError, ZoneInfoNotFoundError):
                raise ToolRejected("wake time or configured timezone is invalid") from None
            local_now = datetime.now(zone)
            target = self._next_local_wake(local_now, hour, minute, zone)
            notice = f"I’m choosing to sleep. I expect to wake at {target.strftime('%Y-%m-%d %H:%M %Z')}."
            self.store.choose_sleep(
                {"sleeping": "1", "sleep_until": target.astimezone(timezone.utc).isoformat(timespec="seconds"),
                 "session_start_requested": ""},
                "Andrew scheduled a local wake time", self.owner_id, notice, datetime.now(timezone.utc), stop_note,
            )
            return f"Andrew is sleeping until {target.strftime('%H:%M')} {self.timezone_name}."
        if name == "continue_activity":
            queued = self.store.pending_message_count(self.owner_id)
            self.store.continue_activity_state()
            return f"Andrew is awake. {queued} owner message(s) remain saved."
        if name == "check_budget":
            if arguments:
                raise ToolRejected("check_budget takes no arguments")
            return self._command("/budget")
        if name in GMAIL_TOOL_NAMES:
            if self.gmail_tools is None:
                raise ToolRejected("Gmail is not configured")
            if name not in {"gmail.status", "gmail.reconcile"}:
                allowed, reason = self.budget_allows(0)
                if not allowed:
                    raise ToolRejected(f"Gmail action blocked by {reason} budget gate")
            return self.gmail_tools.execute(name, arguments)
        if name == "recent_activity":
            limit = arguments.get("limit", 8)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
                raise ToolRejected("activity limit must be from 1 to 8")
            rows = self.store.recent_activity(limit)
            return "Recent objective activity (event ID — summary):\n" + (
                "\n".join(f"{row['id']} — {row['event']}" for row in rows)
                or "No objective events are available."
            )
        if name in {"python.run", "node.run", "shell.run", "browser.open", "browser.control", "browser.account"}:
            allowed, reason = self.budget_allows(0)
            if not allowed:
                raise ToolRejected(f"external action blocked by {reason} budget gate")
            if self.tool_runtime is None:
                raise ToolRejected("isolated runtime is unavailable")
            return self.tool_runtime.execute(name, arguments)
        if name == "save_diary_entry":
            if not self.autonomous_enabled or not self.store.get_state("active_session_id", ""):
                # This precondition rejects the request before any diary write.
                # Return the rejection so durable dispatch can save its exact
                # outcome instead of leaving an admitted, apparently lost tool.
                return json.dumps({"status": "error", "error": "diary_requires_autonomous_session",
                    "executed": False, "message": "Diary entry was not saved. This tool requires an active autonomous session; answer the owner without saving a diary."})
            try:
                content = validate_diary_content(arguments.get("content"))
            except ValueError:
                # Pure text validation only; no diary storage has been called.
                raise ToolRejected('diary content was rejected; use safe text within 3000 characters') from None
            event_ids = arguments.get("event_ids", [])
            if (not isinstance(event_ids, list) or len(event_ids) > 20
                    or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in event_ids)):
                raise ToolRejected("activity event references are invalid")
            local_date = datetime.now(ZoneInfo(self.timezone_name)).date().isoformat()
            try:
                entry_id = self.store.create_diary_entry(
                    local_date, content, session_id=self.store.get_state("active_session_id", ""),
                    event_ids=event_ids,
                )
            except ToolRejected:
                raise
            except ValueError:
                raise ValueError("diary entry was not saved; check the date and event references") from None
            self.store.add_activity(f"Diary entry saved ({entry_id})")
            return "Diary entry saved for today. It will be delivered to the owner by Telegram."
        raise ToolRejected("unsupported controller action")

    def _cancel_active_turn(self):
        cancel = getattr(self.model, 'cancel_active_turn', None)
        if callable(cancel):
            try:
                cancel()
            except Exception as error:
                logging.warning('Active turn cancellation failed (%s)', type(error).__name__)

    def _health(self):
        queues = self.store.health_snapshot(self.owner_id)
        runtime = self.health_snapshot() if self.health_snapshot is not None else {}
        def duration(value):
            return 'unavailable' if value is None else f'{value:.1f}s'
        lines = ['Health (local snapshots)',
                 f"Lifecycle: paused={'yes' if queues['paused'] else 'no'}, "
                 f"sleeping={'yes' if queues['sleeping'] else 'no'}, "
                 f"model halted={'yes' if queues['model_halted'] else 'no'}",
                 f"Queued messages: {queues['queued_messages']}; active={queues['active_messages']}; held={queues['held_messages']}",
                 f"Inbox: {queues['inbox']}; held={queues['held_inbox']}; oldest={duration(queues['inbox_oldest_seconds'])}"]
        for name in ('model', 'diary'):
            worker = runtime.get(name) or {}
            lines.append(f"{name.title()}: {worker.get('state', 'unavailable')}; "
                         f"active={duration(worker.get('active_seconds'))}; "
                         f"last completed={worker.get('last_completion_at') or 'unavailable'}")
        for lane in ('normal', 'system'):
            row = queues['outbox'][lane]
            lines.append(f"Replies {lane}: pending={row['pending']}, sending={row['sending']}, held={row['held']}; "
                         f"oldest pending={duration(row['oldest_seconds'])}")
        lines.append(f"Diary: pending={queues['diary_pending']}, uncertain={queues['diary_uncertain']}")
        for name, row in sorted(runtime.get('metrics', {}).items()):
            lines.append(f"{name}: last={duration(row['last_seconds'])}, active={duration(row['oldest_active_seconds'])}, "
                         f"failures={row['failures']}; last success age={duration(row['last_success_age_seconds'])}")
        return '\n'.join(lines)

    def _status(self):
        with self.store._lock:
            state = ("paused" if self.store.get_state("paused", "0") == "1" else
                     "sleeping" if self.store.get_state("sleeping", "0") == "1" else
                     "awake; automatic activity stopped after an error" if self.store.get_state('autonomy_error', '') else
                     "awake, waiting for your follow-up" if self.store.conversation_waiting(datetime.now(timezone.utc)) else
                     "awake, working independently" if self.store.get_state('active_session_id', '') else "awake")
            _, day = self._periods()
            cycles = self.store.cycle_count(day)
            wake_at = self.store.get_state("sleep_until", "")
            if wake_at:
                try:
                    wake_display = datetime.fromisoformat(wake_at).astimezone(ZoneInfo(self.timezone_name)).strftime("%Y-%m-%d %H:%M %Z")
                except (ValueError, ZoneInfoNotFoundError):
                    wake_display = "unavailable"
            else:
                wake_display = "not set"
            session = self.store.get_state("active_session_status", "idle")
            if self.store.get_state('autonomy_error', ''):
                session = 'error (' + self.store.get_state('autonomy_error') + ')'
            queues = self.store.health_snapshot(self.owner_id)
            uncertain = queues['held_messages']
            if (self.store.get_state('model_turn_origin', '') == 'owner'
                    and not self.store.get_state('autonomy_error','')
                    and self.store.get_state('sleeping', '0') != '1'
                    and self.store.get_state('paused', '0') != '1'):
                state = 'awake, preparing a reply to you'
            latest_diary = self.store.diary_entry()
            diary_state = (f"{latest_diary['local_date']} ({latest_diary['delivery_state']})"
                           if latest_diary else "none")
            schedule = "enabled" if self.autonomous_enabled else "disabled"
            fallback = "enabled" if self.fallback_enabled else "disabled"
            letter_status = ''
            if self.store.get_state('founding_letter_record_id', ''):
                receipt = ('receipt verified' if self.store.get_state('founding_letter_delivered', '0') == '1'
                           else 'delivery pending')
                letter_status = f' Owner letter: {receipt}.'
            from .recovery_records import RecoveryRecords
            waiting = RecoveryRecords(self.store).waiting_provider()
            provider_status = ''
            if waiting:
                retry_time = datetime.fromtimestamp(waiting['retry_at_ms']/1000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                unreached = waiting.get('reason') == 'provider_not_reached'
                cause = 'Provider not reached (request was not sent)' if unreached else 'Provider rate limited (HTTP 429)'
                provider_status = f" {cause}; retry eligible at {retry_time}; rejected attempts: {waiting['attempt']}."
                if not self.store.get_state('autonomy_error','') and self.store.get_state('paused', '0')!='1' and self.store.get_state('sleeping', '0')!='1':
                    state = 'awake, waiting to reconnect to the provider' if unreached else 'awake, waiting for the provider rate limit'
            return (f"Andrew is {state}. Session: {session}. Autonomous schedule: {schedule}; fallback: {fallback}. "
                    f"Next self-scheduled wake: {wake_display}. "
                    f"Autonomous cycles today: {cycles}. Queued messages: {queues['queued_messages']}; "
                    f"processing: {queues['active_messages']}; held from interrupted attempts: {uncertain}. "
                    f"Latest diary: {diary_state}." + self._reply_progress() + provider_status + letter_status)

    def _reply_progress(self):
        if self.store.get_state('autonomy_error','') or any(self.store.get_state(key,'0')=='1' for key in ('paused','sleeping')):
            return ''
        try:
            progress = json.loads(self.store.get_state('model_turn_progress', '') or '{}')
            phase = progress.get('phase')
            if phase not in {'starting conversation', 'running a tool', 'waiting for the next model step',
                             'waiting for the provider', 'processing the model response'}:
                return ''
            age = max(0, datetime.now(timezone.utc).timestamp() - float(progress['at']))
            return f' Reply progress: {phase}; last change {age:.0f}s ago.'
        except (ValueError, TypeError, KeyError):
            return ''

    def _command(self, command, text="", update_id=None):
        if command in {'/media_status', '/media_budget', '/voice', '/image', '/image_edit', '/media_cancel', '/voice_on', '/voice_off', '/avatar_propose', '/avatar_approve'}:
            if self.media is None:
                return 'Media is unavailable.'
            if command in {'/media_status', '/media_budget'}:
                return json.dumps(self.media.status(), ensure_ascii=False)
            if command in {'/voice_on', '/voice_off'}:
                if command == '/voice_on' and not self.media.policy.voice_enabled:
                    return 'Voice generation is disabled in the service configuration.'
                self.store.set_state('media_spontaneous_voice', '1' if command == '/voice_on' else '0')
                return ('TTS is on. Andrew may choose to send voice notes spontaneously.'
                        if command == '/voice_on' else 'Spontaneous TTS is off. Explicit /voice requests remain available.')
            argument = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ''
            if command in {'/avatar_propose', '/avatar_approve'}:
                try:
                    if command == '/avatar_propose':
                        return 'Avatar proposal: ' + self.media.propose_avatar(argument)
                    return 'Avatar approved for background publication.' if self.media.approve_avatar(argument) else 'Proposal is unavailable or already attempted.'
                except ValueError as error:
                    return str(error)
            if command == '/media_cancel':
                return 'Media job cancelled.' if self.media.cancel(argument) else 'Job cannot be cancelled; running or uncertain calls are never retried automatically.'
            if update_id is None:
                return 'Media generation requires a saved owner command.'
            try:
                request_id = 'telegram:' + str(update_id)
                reference_id = None
                if command == '/image_edit':
                    parts = argument.split(maxsplit=1)
                    if len(parts) != 2:
                        return 'Use /image_edit <image_job_id> <edit instruction>.'
                    reference_id, argument = parts
                job = self.media.submit('voice' if command == '/voice' else 'image', argument, request_id,
                                        owner_requested=True, reference_id=reference_id)
                return f'Media job queued: {job}; owner request: {request_id}'
            except ValueError as error:
                return str(error)
        if command == '/health':
            return self._health()
        if command == '/research':
            argument = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ''
            if argument == 'events':
                rows = self.store.recent_research_observations()
                return ('Research observations (controller records; findings remain self-reported):\n' +
                        ('\n'.join(f"{row['created_at']} · {row['origin']} · {row['kind']} · "
                                   f"{row['tool_name'] or row['entry_id'] or 'session'} · {row['outcome']}" for row in rows)
                         or 'No observations yet.'))
            if argument:
                try:
                    return json.dumps(self.research.execute('get', {'id': argument}), ensure_ascii=False)
                except ValueError:
                    return 'Use /research for unfinished questions, /research <id> for details, or /research events.'
            page = self.research.execute('list', {'limit': 8})
            return ('Andrew’s unfinished questions (his own journal, not assigned tasks):\n' +
                    ('\n'.join(f"{row['record_id']} · {row['status']} · {row['question']}" for row in page['entries'])
                     or 'No saved questions yet.') + '\nUse /research <id> for details or /research events for activity.')
        if command in {"/help", "/start", "/menu"}:
            if command in {"/start", "/menu"}:
                return ("Hello, I’m Andrew, your AI assistant.\n\n"
                        "Choose a button below to check my activity, browsing history or budget, "
                        "or manage when I’m awake. You can also send me a message.\n\n"
                        "Tap ❓ Help for command details. Opening this menu does not wake me.")
            return HELP_TEXT
        if command == "/cancel":
            self.store.set_state("telegram_wake_prompt_until", "")
            return "Time entry cancelled. Choose a button or send me a message."
        if command == "/gmail":
            if self.gmail_tools is None:
                return "Gmail is not configured. Connect it using the administrator OAuth command."
            if len(text.split()) > 1:
                return "Use /gmail to view permissions. Permission changes require the administrator command; never paste OAuth credentials in chat."
            try:
                status = self.gmail_tools.status()
            except ValueError:
                return "Gmail configuration could not be read. Check the administrator setup."
            lines = ["📬 Gmail", "Connected: " + ("yes" if status['connected'] else "no"),
                     "Enabled: " + ("yes" if status['enabled'] else "no")]
            for name, flags in status['permissions'].items():
                state = "enabled" if flags['effective'] else "disabled"
                if flags['allowed_by_owner'] and not flags['oauth_scope_present']:
                    state += " — OAuth consent required"
                lines.append(f"{name.capitalize()}: {state}")
            lines.append("Delete moves mail to Trash. Permanent deletion is unavailable.")
            return "\n".join(lines)
        if command == "/history":
            if self.visit_history is None:
                return "Browser history is unavailable."
            parts = text.split(maxsplit=2)
            if len(parts) >= 2 and parts[1].lower() == "delete":
                if len(parts) != 3:
                    return "Use /history delete <visit id> or /history delete all."
                if parts[2].lower() == "all":
                    self.store.set_state("history_delete_confirmation", "pending")
                    return "To permanently delete all browser history, send /history confirm DELETE ALL HISTORY."
                try:
                    deleted = self.visit_history.delete(int(parts[2]))
                except (ValueError, TypeError):
                    return "Visit ID must be a positive number."
                self.store.add_activity("Owner deleted one browser history visit" if deleted else "Owner requested deletion of a missing browser history visit")
                return "Visit deleted." if deleted else "That visit was not found."
            if len(parts) >= 2 and parts[1].lower() == "confirm":
                if len(parts) != 3 or parts[2] != "DELETE ALL HISTORY" or self.store.get_state("history_delete_confirmation", "") != "pending":
                    return "No matching full-history deletion confirmation is pending."
                count = self.visit_history.delete_all()
                self.store.set_state("history_delete_confirmation", "")
                self.store.add_activity("Owner deleted all browser history")
                return f"Deleted {count} browser history visit(s)."
            command_and_query = text.split(maxsplit=1)
            query = command_and_query[1] if len(command_and_query) == 2 else ""
            try:
                rows = self.visit_history.search(query, limit=20)
            except (ValueError, OSError):
                return "Browser history could not be read."
            if not rows:
                return "No browser history visits matched."
            return "Browser history (newest first):\n" + "\n".join(
                f"{row['id']} · {row['visited_at']} · {row['title']} · {row['url']}" for row in rows
            )
        if command == "/sleep":
            pieces = text.split(maxsplit=1)
            wake_at = ""
            if len(pieces) == 2:
                try:
                    minutes = int(pieces[1])
                except ValueError:
                    return "Use /sleep or /sleep followed by a number of minutes."
                if not 1 <= minutes <= 1440:
                    return "Sleep duration must be between 1 and 1440 minutes."
                wake_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
            self.store.transition_owner_control(
                {"sleeping": "1", "sleep_until": wake_at, "session_start_requested": "", "autonomy_error": "",
                 "active_session_id": "", "active_session_started_at": "", "active_session_status": "owner_sleep"},
                "Owner requested sleep",
            )
            self._cancel_active_turn()
            if wake_at:
                return f"I’m going to sleep and will wake in {minutes} minutes. I won’t make model requests while asleep."
            if self.fallback_enabled:
                return f"I’m going to sleep until you wake me or my next daily fallback at {self.fallback_wake_time} {self.timezone_name}."
            return "I’m going to sleep. I won’t make model requests until you wake me."
        if command == "/wake_at":
            pieces = text.split(maxsplit=1)
            if len(pieces) != 2:
                self.store.set_state("telegram_wake_prompt_until", (
                    datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat())
                return (f"⏰ What time should I wake up?\n\n"
                        f"Send a 24-hour time, such as 09:00 ({self.timezone_name}). "
                        "This will put me to sleep until that time; an owner pause stays in place.\n\n"
                        "Send /cancel to return to chat. This prompt expires in 10 minutes.")
            try:
                hour_text, minute_text = pieces[1].split(":", 1)
                hour, minute = int(hour_text), int(minute_text)
                if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                    raise ValueError
                zone = ZoneInfo(self.timezone_name)
            except (ValueError, ZoneInfoNotFoundError):
                return "Use a valid local 24-hour time, such as /wake_at 09:00."
            now_local = datetime.now(zone)
            target = self._next_local_wake(now_local, hour, minute, zone)
            self.store.transition_owner_control(
                {"sleeping": "1", "sleep_until": target.astimezone(timezone.utc).isoformat(timespec="seconds"),
                 "session_start_requested": "", "active_session_id": "", "active_session_started_at": "",
                 "active_session_status": "owner_sleep", "autonomy_error": ""},
                "Andrew scheduled a local wake time",
            )
            self._cancel_active_turn()
            return f"I’m going to sleep and will wake at {target.strftime('%H:%M')} {self.timezone_name}."
        if command == "/wake":
            self.store.transition_owner_control(
                {"sleeping": "0", "paused": "0", "sleep_until": "", "autonomy_error": "", "conversation_until": "",
                 "session_start_requested": "owner_wake" if self.autonomous_enabled else "",
                 "active_session_id": "", "active_session_started_at": "",
                 "active_session_status": "owner_wake"}, "Owner woke Andrew"
            )
            self._cancel_active_turn()
            queues = self.store.health_snapshot(self.owner_id)
            return (f"I’m awake. Messages waiting: {queues['queued_messages']}. "
                    f"Held from interrupted attempts: {queues['held_messages']}; these are not replayed automatically.")
        if command == "/pause":
            self.store.transition_owner_control(
                {"paused": "1", "session_start_requested": "", "active_session_id": "",
                 "active_session_started_at": "", "active_session_status": "paused"}, "Owner paused Andrew"
            )
            self._cancel_active_turn()
            return "Paused. I won’t make model requests until you use /wake."
        if command == "/replay":
            count = self.store.retry_uncertain_messages(self.owner_id)
            if not count:
                return "There are no messages held with an uncertain prior delivery."
            self.store.add_activity(f"Owner requested retry of {count} uncertain queued message(s)")
            self.store.transition_owner_control(
                {'autonomy_error': '', 'active_session_status': 'owner_replay'},
                'Owner explicitly authorized uncertain message retry',
            )
            return (f"Requeued {count} message(s). Their previous delivery may have reached Andrew, "
                    "so a retry could produce a duplicate answer.")
        if command == "/status":
            return self._status()
        if command == "/budget":
            snapshot = budget_snapshot(
                self.store,
                monthly_limit_usd=self.monthly_limit_usd,
                monthly_reserve_usd=self.monthly_reserve_usd,
                daily_target_usd=self.daily_target_usd,
            )
            credits = self.credit_reader() if self.credit_reader else {'available': False, 'reason': 'credit_reader_unavailable'}
            return json.dumps({'policy': 'Andrew manages API credits; no controller spending caps',
                               'reasoning_usage': snapshot, 'provider_credits': credits,
                               'media': self.media.status() if self.media else None}, ensure_ascii=False)
        if command == "/activity":
            entries = self.store.recent_activity(8)
            return "Recent objective activity (event ID — summary):\n" + (
                "\n".join(f"{row['id']} — {row['event']}" for row in entries) or "No activity yet."
            )
        if command == "/diary":
            try:
                kind, local_date = parse_diary_command(text)
            except ValueError as error:
                return str(error)
            if kind == "retry":
                entry = self.store.uncertain_diary_delivery(local_date)
                if not entry:
                    return f"No uncertain diary delivery exists for {local_date}."
                if not self.store.retry_uncertain_diary_delivery(entry["entry_id"]):
                    return "The diary delivery state changed; check /status before retrying."
                self.store.add_activity(f"Owner explicitly retried diary delivery ({entry['entry_id']})")
                return f"Retry queued for {local_date}. Telegram may send the entry twice if its earlier delivery succeeded."
            entry = self.store.diary_entry(local_date)
            if not entry:
                return f"No diary entry exists for {local_date}." if local_date else "No diary entries yet."
            return format_diary_entry(entry)
        return "Unknown command. Use /help."
