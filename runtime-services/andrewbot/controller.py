from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .budget import budget_snapshot
from .configuration import ModelConfig, load_model_config
from .diary import format_diary_entry, parse_diary_command, validate_diary_content
from .telegram_menu import BUTTON_COMMANDS, HELP_TEXT


class Controller:
    def __init__(self, store, model, owner_id, monthly_limit_usd=10.0,
                 monthly_reserve_usd=1.0, daily_target_usd=0.30,
                 timezone_name="America/Toronto", fallback_wake_time="09:00",
                 autonomous_enabled=False, fallback_enabled=False, memory_service=None, model_config=None,
                 tool_runtime=None, visit_history=None, gmail_tools=None):
        self.store = store
        self.model = model
        self.owner_id = int(owner_id)
        self.monthly_limit_usd = float(monthly_limit_usd)
        self.monthly_reserve_usd = float(monthly_reserve_usd)
        self.daily_target_usd = float(daily_target_usd)
        self.timezone_name = timezone_name
        self.fallback_wake_time = fallback_wake_time
        self.autonomous_enabled = bool(autonomous_enabled)
        self.fallback_enabled = bool(fallback_enabled) and self.autonomous_enabled
        self.memory_service = memory_service
        self.tool_runtime = tool_runtime
        self.visit_history = visit_history
        self.gmail_tools = gmail_tools
        if model_config is not None and not isinstance(model_config, ModelConfig):
            raise ValueError("model configuration is invalid")
        self.model_config = model_config or load_model_config({})

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
        snapshot = budget_snapshot(
            self.store,
            monthly_limit_usd=self.monthly_limit_usd,
            monthly_reserve_usd=self.monthly_reserve_usd,
            daily_target_usd=self.daily_target_usd,
        )
        if estimated_cost_usd > snapshot["remaining_ordinary"]:
            return False, "monthly_reserve"
        return True, "ok"

    def handle_update(self, update):
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
        text = text.strip()
        text = BUTTON_COMMANDS.get(text, text)
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command.startswith("/"):
            self.store.set_state("telegram_wake_prompt_until", "")
            return self._command(command, text)
        prompt_until = self.store.get_state("telegram_wake_prompt_until", "")
        if prompt_until:
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
        self.store.add_message(self.owner_id, "user", text)
        messages = self._bounded_messages([
            {"role": row["role"], "content": row["content"]}
            for row in self.store.recent_messages(self.owner_id)
        ])
        max_cost = self._estimate_cost(messages)
        allowed, reason = self.budget_allows(max_cost)
        if not allowed:
            self.store.add_activity(f"Model request blocked by {reason}")
            return "I’m pausing model requests to stay within the configured budget."
        try:
            reply, usage = self.model.reply(messages)
        except Exception:
            self.store.add_activity("Model request failed")
            return "I couldn’t reach my model just now. Your message is saved; please try again shortly."
        month, day = self._periods()
        actual = self._actual_cost(usage)
        if not usage.get("accounted_by_gateway"):
            self.store.record_usage(month, day, max(actual, max_cost if not usage else 0.0),
                                    int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
        self.store.add_message(self.owner_id, "assistant", reply)
        self.store.add_activity("Andrew replied to the owner")
        return reply

    def _estimate_cost(self, messages):
        # Conservative byte-based bound and bounded output keep each request reservable.
        input_bytes = sum(len(item["content"].encode("utf-8")) for item in messages)
        input_tokens = min(48000, max(1, input_bytes))
        return (input_tokens * self.model_config.input_usd_per_million
                + 512 * self.model_config.output_usd_per_million) / 1_000_000

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
        due = datetime.fromisoformat(wake_at)
        if due.tzinfo is None:
            raise ValueError("stored wake time must be timezone-aware")
        if current.astimezone(timezone.utc) < due.astimezone(timezone.utc):
            return False
        released = self.store.pending_message_count(self.owner_id)
        self.store.transition_state(
            {"sleeping": "0", "sleep_until": "", "session_start_requested": "scheduled_wake"},
            f"Scheduled wake completed; {released} queued message(s) pending",
        )
        return True

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
        if self.store.get_state("sleeping", "0") == "1" or self.store.get_state("paused", "0") == "1":
            return None
        queued = self.store.claim_queued_messages(self.owner_id)
        if not queued:
            return None
        message = "Messages received while I was asleep:\n" + "\n".join(row["content"] for row in queued)
        bounded = self._bounded_messages([{"role": "user", "content": message}])
        estimate = self._estimate_cost(bounded)
        allowed, reason = self.budget_allows(estimate)
        if not allowed:
            self.store.restore_queued_claim([row["id"] for row in queued])
            self.store.add_activity(f"Queued messages held by {reason} budget gate")
            return None
        try:
            reply, usage = self.model.reply(bounded)
        except Exception:
            self.store.hold_uncertain_queued_claim([row["id"] for row in queued])
            self.store.add_activity("Queued message replay outcome uncertain; messages held to avoid duplicate delivery")
            return None
        month, day = self._periods()
        if not usage.get("accounted_by_gateway"):
            actual = self._actual_cost(usage)
            self.store.record_usage(month, day, max(actual, estimate if not usage else 0.0),
                                    int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
        self.store.release_queued(self.owner_id, [row["id"] for row in queued])
        self.store.add_message(self.owner_id, "assistant", reply)
        self.store.add_activity(f"Andrew replayed {len(queued)} queued message(s)")
        return reply

    def execute_tool(self, name, arguments):
        if not isinstance(arguments, dict):
            if name in {"memory_add_experience", "memory_propose_personality_edit"} and self.memory_service:
                kind = "personality" if name == "memory_propose_personality_edit" else "experience"
                self.memory_service.reject_edit_attempt(kind)
            raise ValueError("tool arguments must be an object")
        if self.store.get_state("paused", "0") == "1" and name not in {"check_budget", "recent_activity", "memory_search", "gmail.status"}:
            raise ValueError("owner pause can only be cleared by the owner with /wake")
        if self.store.get_state("sleeping", "0") == "1" and name not in {"check_budget", "recent_activity", "memory_search", "gmail.status"}:
            raise ValueError("sleep can only end at a scheduled wake or an owner wake")
        if (name in {"sleep", "wake_at"} and self.store.get_state("active_session_id", "")
                and not self.store.diary_entry(datetime.now(ZoneInfo("America/Toronto")).date().isoformat())):
            raise ValueError("save today's diary before requesting autonomous sleep")
        if name in {"memory_search", "memory_add_experience", "memory_propose_personality_edit"}:
            if self.memory_service is None:
                raise ValueError("memory service is unavailable")
            if name == "memory_search":
                rows = self.memory_service.search(arguments.get("query"), arguments.get("limit", 8))
                if not rows:
                    return "No relevant memory records were found."
                result = "Relevant memory records (read-only):\n" + "\n".join(
                    f"{row['record_type']} {row['record_id']} ({row['created_at']}; source={row['source']}): "
                    f"{row['content']} [events={row['event_ids']}; diary={row['diary_ids']}]"
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
                )
                return f"Memory record saved ({record_id})."
            revision_id = self.memory_service.propose_personality_edit(
                arguments.get("content"), arguments.get("reason"),
                arguments.get("event_ids", []), arguments.get("diary_ids", []),
            )
            return f"Personality revision saved ({revision_id}); previous revisions are preserved."
        if name == "sleep":
            minutes = arguments.get("minutes")
            if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:
                raise ValueError("sleep duration must be an integer from 1 to 1440 minutes")
            wake_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
            self.store.transition_state(
                {"sleeping": "1", "sleep_until": wake_at, "session_start_requested": ""},
                "Andrew requested timed sleep",
            )
            return f"Andrew is sleeping and will wake in {minutes} minutes."
        if name == "wake_at":
            value = arguments.get("time")
            if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
                raise ValueError("wake time must use local HH:MM format")
            try:
                hour, minute = (int(piece) for piece in value.split(":"))
                if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                    raise ValueError
                zone = ZoneInfo(self.timezone_name)
            except (ValueError, ZoneInfoNotFoundError):
                raise ValueError("wake time or configured timezone is invalid") from None
            local_now = datetime.now(zone)
            target = self._next_local_wake(local_now, hour, minute, zone)
            self.store.transition_state(
                {"sleeping": "1", "sleep_until": target.astimezone(timezone.utc).isoformat(timespec="seconds"),
                 "session_start_requested": ""},
                "Andrew scheduled a local wake time",
            )
            return f"Andrew is sleeping until {target.strftime('%H:%M')} {self.timezone_name}."
        if name == "continue_activity":
            queued = self.store.pending_message_count(self.owner_id)
            self.store.transition_state(
                {"sleeping": "0", "sleep_until": "",
                 "session_start_requested": "agent_continue"}, "Andrew continued activity"
            )
            return f"Andrew is awake. {queued} queued message(s) were released."
        if name == "check_budget":
            if arguments:
                raise ValueError("check_budget takes no arguments")
            return self._command("/budget")
        if name in {"gmail.status", "gmail.read", "gmail.draft", "gmail.send", "gmail.delete"}:
            if self.gmail_tools is None:
                raise ValueError("Gmail is not configured")
            if name != "gmail.status":
                allowed, reason = self.budget_allows(0)
                if not allowed:
                    raise ValueError(f"Gmail action blocked by {reason} budget gate")
            return self.gmail_tools.execute(name, arguments)
        if name == "recent_activity":
            limit = arguments.get("limit", 8)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
                raise ValueError("activity limit must be from 1 to 8")
            rows = self.store.recent_activity(limit)
            return "Recent objective activity (event ID — summary):\n" + (
                "\n".join(f"{row['id']} — {row['event']}" for row in rows)
                or "No objective events are available."
            )
        if name in {"python.run", "node.run", "shell.run", "browser.open", "browser.control", "browser.account"}:
            allowed, reason = self.budget_allows(0)
            if not allowed:
                raise ValueError(f"external action blocked by {reason} budget gate")
            if self.tool_runtime is None:
                raise ValueError("isolated runtime is unavailable")
            return self.tool_runtime.execute(name, arguments)
        if name == "save_diary_entry":
            if not self.autonomous_enabled or not self.store.get_state("active_session_id", ""):
                raise ValueError("diary entries can only be saved during an active autonomous session")
            content = validate_diary_content(arguments.get("content"))
            event_ids = arguments.get("event_ids", [])
            if (not isinstance(event_ids, list) or len(event_ids) > 20
                    or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in event_ids)):
                raise ValueError("activity event references are invalid")
            local_date = datetime.now(ZoneInfo("America/Toronto")).date().isoformat()
            try:
                entry_id = self.store.create_diary_entry(
                    local_date, content, session_id=self.store.get_state("active_session_id", ""),
                    event_ids=event_ids,
                )
            except ValueError:
                raise ValueError("diary entry was not saved; check the date and event references") from None
            self.store.add_activity(f"Diary entry saved ({entry_id})")
            return "Diary entry saved for today. It will be delivered to the owner by Telegram."
        raise ValueError("unsupported controller action")

    def _command(self, command, text=""):
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
            query = text[len("/history"):].strip()
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
            self.store.transition_state(
                {"sleeping": "1", "sleep_until": wake_at, "session_start_requested": ""},
                "Owner requested sleep",
            )
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
            self.store.transition_state(
                {"sleeping": "1", "sleep_until": target.astimezone(timezone.utc).isoformat(timespec="seconds"),
                 "session_start_requested": ""},
                "Andrew scheduled a local wake time",
            )
            return f"I’m going to sleep and will wake at {target.strftime('%H:%M')} {self.timezone_name}."
        if command == "/wake":
            self.store.transition_state(
                {"sleeping": "0", "paused": "0", "sleep_until": "",
                 "session_start_requested": "owner_wake" if self.autonomous_enabled else "",
                 "active_session_id": "", "active_session_started_at": "",
                 "active_session_status": "owner_wake"}, "Owner woke Andrew"
            )
            queued = self.store.pending_message_count(self.owner_id)
            return f"I’m awake. I saved {queued} message(s) while sleeping."
        if command == "/pause":
            self.store.transition_state(
                {"paused": "1", "session_start_requested": "", "active_session_id": "",
                 "active_session_started_at": "", "active_session_status": "paused"}, "Owner paused Andrew"
            )
            return "Paused. I won’t make model requests until you use /wake."
        if command == "/replay":
            count = self.store.retry_uncertain_messages(self.owner_id)
            if not count:
                return "There are no messages held with an uncertain prior delivery."
            self.store.add_activity(f"Owner requested retry of {count} uncertain queued message(s)")
            return (f"Requeued {count} message(s). Their previous delivery may have reached Andrew, "
                    "so a retry could produce a duplicate answer.")
        if command == "/status":
            state = "sleeping" if self.store.get_state("sleeping", "0") == "1" else "paused" if self.store.get_state("paused", "0") == "1" else "awake"
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
            uncertain = self.store.uncertain_message_count(self.owner_id)
            latest_diary = self.store.diary_entry()
            diary_state = (f"{latest_diary['local_date']} ({latest_diary['delivery_state']})"
                           if latest_diary else "none")
            schedule = "enabled" if self.autonomous_enabled else "disabled"
            fallback = "enabled" if self.fallback_enabled else "disabled"
            return (f"Andrew is {state}. Session: {session}. Autonomous schedule: {schedule}; fallback: {fallback}. "
                    f"Next self-scheduled wake: {wake_display}. "
                    f"Autonomous cycles today: {cycles}/150. Queued messages: {self.store.pending_message_count(self.owner_id)} "
                    f"({uncertain} with uncertain prior delivery). Latest diary: {diary_state}.")
        if command == "/budget":
            snapshot = budget_snapshot(
                self.store,
                monthly_limit_usd=self.monthly_limit_usd,
                monthly_reserve_usd=self.monthly_reserve_usd,
                daily_target_usd=self.daily_target_usd,
            )
            cycles = self.store.cycle_count(snapshot["day"])
            return (f"This month: ${snapshot['monthly_cost']:.4f} of ${self.monthly_limit_usd:.2f}. "
                    f"Protected reserve: ${self.monthly_reserve_usd:.2f}. Spent today: ${snapshot['daily_cost']:.4f}. "
                    f"Month allowance accrued: ${snapshot['accrued_allowance']:.2f}; remaining ordinary allowance: "
                    f"${snapshot['remaining_ordinary']:.4f}. Autonomous cycles: {cycles}/150.")
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
