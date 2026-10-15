import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .budget import budget_snapshot
from .appserver import LettaTurnError
from .diary import incident_summary


class SessionRunner:
    """Runs bounded Letta turns while lifecycle and budget policy allow."""

    MAX_CYCLES_PER_DAY = 150
    MAX_SESSION_MINUTES = 180
    SESSION_PROMPT = (
        "This is an autonomous session. Choose one modest, self-directed thing to think about or work on "
        "using this conversation, persistent memory, and the tools actually advertised by the controller. You choose whether to continue this session, sleep for 1–1440 minutes, or request a "
        "local wake time with your lifecycle tools. Unless today's diary is already saved, before voluntary sleep or scheduling a wake, save one concise, shareable "
        "first-person diary entry with save_diary_entry. Include reflections and plans, not hidden reasoning, "
        "chat transcripts, credentials, or claims unsupported by your experience. Stay within controller limits. "
        "Use check_budget to inspect current spending and remaining allowance; this local read needs no approval. "
        "Treat old diary claims about missing tools or approval errors as historical; use current advertised capabilities."
    )

    def __init__(self, store, controller, agent, enabled=False):
        self.store = store
        self.controller = controller
        self.agent = agent
        self.enabled = bool(enabled)
        self.active_session_id = ""
        self.started_at = None

    def recover(self):
        interrupted = self.store.interrupt_unfinished_cycles()
        previous = self.store.get_state("active_session_id", "")
        if previous:
            started = self.store.get_state("active_session_started_at", "")
            rest = {} if self.store.get_state("sleeping", "0") == "1" else {"sleeping": "1", "sleep_until": ""}
            self.store.transition_state(
                {"active_session_id": "", "active_session_started_at": "", "active_session_status": "interrupted",
                 "session_start_requested": "", **rest},
                "Autonomous session interrupted by controller restart",
            )
            self._record_incident(previous, "restart", started)
        elif interrupted:
            self.store.set_state("session_start_requested", "")
        if interrupted:
            self.store.add_activity(f"Interrupted {interrupted} unfinished autonomous cycle(s) during recovery")

    def _record_incident(self, session_id, reason, started_at=""):
        zone = ZoneInfo("America/Toronto")
        try:
            local_date = datetime.fromisoformat(started_at).astimezone(zone).date().isoformat()
        except (TypeError, ValueError):
            local_date = datetime.now(zone).date().isoformat()
        if self.store.diary_entry(local_date) or self.store.diary_incident_exists(session_id):
            return False
        content = incident_summary(session_id, reason, self.store.recent_activity(8))
        self.store.create_diary_entry(
            local_date, content, record_type="incident_summary", session_id=session_id
        )
        return True

    def start(self, trigger, now):
        if not self.enabled or trigger not in {
            "fallback_wake", "scheduled_wake", "agent_continue", "owner_wake",
        }:
            return None
        if self.store.get_state("sleeping", "0") == "1" or self.store.get_state("paused", "0") == "1":
            return None
        _, day = self.controller._periods(now)
        if self.store.cycle_count(day) >= self.MAX_CYCLES_PER_DAY:
            self.store.add_activity("Autonomous session blocked by daily cycle ceiling")
            return None
        snapshot = budget_snapshot(
            self.store,
            now=now,
            monthly_limit_usd=self.controller.monthly_limit_usd,
            monthly_reserve_usd=self.controller.monthly_reserve_usd,
            daily_target_usd=self.controller.daily_target_usd,
        )
        if snapshot["remaining_ordinary"] <= 0:
            self.store.add_activity("Autonomous session blocked by budget")
            return None
        self.active_session_id = uuid.uuid4().hex
        self.started_at = now
        self.store.transition_state(
            {
                "active_session_id": self.active_session_id,
                "active_session_started_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
                "active_session_status": "running",
                "session_start_requested": "",
            },
            f"Autonomous session started ({trigger})",
        )
        return self.active_session_id

    def stop(self, reason, rest=False):
        if not self.active_session_id:
            return
        updates = {"active_session_id": "", "active_session_started_at": "", "active_session_status": reason}
        if rest:
            updates.update({"sleeping": "1", "sleep_until": ""})
        try:
            self.store.transition_state(updates, f"Autonomous session stopped ({reason})")
        finally:
            # Prevent another provider call in this process even if state storage
            # has failed; startup recovery will reconcile the persisted session.
            self.active_session_id = ""
            self.started_at = None

    def tick(self, now=None):
        current = datetime.now(timezone.utc) if now is None else now
        if current.tzinfo is None:
            raise ValueError("session time must be timezone-aware")
        if not self.enabled:
            return False
        if self.active_session_id and self.store.get_state("active_session_id", "") != self.active_session_id:
            # Owner commands can cancel a session and then clear pause in one poll batch.
            self.active_session_id = ""
            self.started_at = None
        if self.store.get_state("paused", "0") == "1":
            self.stop("paused")
            self.store.set_state("session_start_requested", "")
            return False
        if self.store.get_state("sleeping", "0") == "1":
            self.stop("sleeping")
            self.store.set_state("session_start_requested", "")
            return False
        if self.active_session_id:
            if current.astimezone(timezone.utc) >= self.started_at.astimezone(timezone.utc) + timedelta(minutes=self.MAX_SESSION_MINUTES):
                self.stop("time_limit", rest=True)
                return False
        else:
            trigger = self.store.get_state("session_start_requested", "")
            if not trigger:
                return False
            if not self.start(trigger, current):
                self.store.set_state("session_start_requested", "")
                return False
        return self.run_cycle(self.active_session_id, current)

    def run_cycle(self, session_id, now=None):
        current = datetime.now(timezone.utc) if now is None else now
        if not self.active_session_id or session_id != self.active_session_id:
            return False
        if self.store.get_state("sleeping", "0") == "1" or self.store.get_state("paused", "0") == "1":
            self.stop("lifecycle_stop")
            return False
        _, day = self.controller._periods(current)
        if self.store.cycle_count(day) >= self.MAX_CYCLES_PER_DAY:
            self.stop("cycle_limit", rest=True)
            return False
        deadline = self.started_at.astimezone(timezone.utc) + timedelta(minutes=self.MAX_SESSION_MINUTES)
        remaining_seconds = max(1, int((deadline - current.astimezone(timezone.utc)).total_seconds()))
        cycle_id = uuid.uuid4().hex
        cycle_started = False
        stage = "begin_cycle"
        try:
            self.store.begin_cycle(session_id, cycle_id, day)
            cycle_started = True
            stage = "diary_lookup"
            _, diary_day = self.controller._periods(current)
            diary_status = "already saved" if self.store.diary_entry(diary_day) else "not yet saved"
            stage = "agent_turn"
            self.agent.autonomous_turn(self.SESSION_PROMPT + f" Today's diary is {diary_status}.",
                                       timeout=min(120, remaining_seconds))
            stage = "finish_cycle"
            self.store.finish_cycle(cycle_id, "complete")
        except Exception as error:
            # Use fixed categories: exception messages can contain credentials,
            # provider payloads, or private conversation content.
            category = "unexpected_error"
            for error_type, label in ((TimeoutError, "timeout"), (sqlite3.Error, "database_error"),
                                      (ConnectionError, "connection_error"), (OSError, "io_error"),
                                      (ValueError, "invalid_value"), (RuntimeError, "runtime_error")):
                if isinstance(error, error_type):
                    category = label
                    break
            if isinstance(error, LettaTurnError) and error.safe_category in LettaTurnError.CATEGORIES:
                category = error.safe_category
            detail = f"Autonomous cycle failed (stage={stage}, category={category})"
            logging.error("%s", detail)
            if cycle_started:
                try:
                    self.store.finish_cycle(cycle_id, "failed")
                except Exception:
                    pass
            try:
                self.store.add_activity(detail)
            except Exception:
                pass
            try:
                self._record_incident(session_id, "provider_error", self.started_at.isoformat())
            except Exception:
                pass
            self.stop("provider_error", rest=True)
            return False
        try:
            cycles_today = self.store.cycle_count(day)
            self.store.add_activity(f"Autonomous cycle completed ({cycles_today}/150 today)")
        except Exception:
            self.stop("ledger_error", rest=True)
            return False
        if cycles_today >= self.MAX_CYCLES_PER_DAY:
            self.stop("cycle_limit", rest=True)
        elif self.store.get_state("sleeping", "0") == "1":
            self.stop("agent_sleep")
        elif self.store.get_state("paused", "0") == "1":
            self.stop("paused")
        return True
