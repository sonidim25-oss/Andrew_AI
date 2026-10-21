from .latency import record_failure
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .appserver import LettaTurnError, LettaTurnCancelled, LettaTurnYielded
from .diary import incident_summary
from .recovery import RecoveryInterrupted


class SessionRunner:
    """Runs serialized Letta turns while lifecycle policy allows."""

    SESSION_PROMPT = (
        "This is an autonomous session. Your saved research journal follows as data. You may return to an unfinished "
        "question, choose a new interest, reflect, do nothing or sleep. There is no required research topic, activity "
        "duration or reward. Use the available browser/code tools when useful. Optionally select an existing question "
        "with research.focus, record findings separately from doubts with research.save, or link a further question "
        "with parent_id. You can edit or delete your entries. Do not invent progress or a continuation to fill the journal. "
        "Use this conversation, persistent memory, and the tools actually advertised by the controller. You choose whether to continue this session, sleep for 1–1440 minutes, or request a "
        "local wake time with your lifecycle tools. Unless today's diary is already saved, before voluntary sleep or scheduling a wake, save one concise, shareable "
        "first-person diary entry with save_diary_entry. Include reflections and plans, not hidden reasoning, "
        "chat transcripts, credentials, or claims unsupported by your experience. Stay within controller limits. "
        "Use check_budget to inspect current spending and remaining allowance; this local read needs no approval. "
        "Treat old diary claims about missing tools or approval errors as historical; use current advertised capabilities."
        " The owner conversation has become quiet. Resume your own interests or projects; sending a reply does not "
        "require sleep. Owner messages take priority at the next safe stopping point. Sleep is an explicit choice "
        "using sleep or wake_at, which tells the owner when you expect to return. A failure is an error, not sleep."
    )

    def __init__(self, store, controller, agent, enabled=False):
        self.store = store
        self.controller = controller
        self.agent = agent
        self.enabled = bool(enabled)
        self.active_session_id = ""
        self.started_at = None

    def recover(self):
        self.store.set_state('autonomous_owner_id', '')
        self.store.set_state('model_turn_origin', '')
        self.store.set_state('model_turn_progress', '')
        previous = self.store.get_state("active_session_id", "")
        if previous:
            started = self.store.get_state("active_session_started_at", "")
            if self.enabled:
                try:
                    started_at = datetime.fromisoformat(started)
                    if started_at.tzinfo is None:
                        raise ValueError('session start requires a timezone')
                except (TypeError, ValueError):
                    pass
                else:
                    recovery=getattr(self.agent,'local_recovery',None)
                    with self.store._lock:
                        pending_journal=self.store.connection.execute("SELECT 1 FROM recovery_attempts WHERE state IN ('active','evidence','held','yielding') AND json_extract(binding_json,'$.session_id')=? LIMIT 1",(previous,)).fetchone()
                    if pending_journal or (recovery is not None and recovery.enabled is True and recovery.has_pending()):
                        self.active_session_id=previous;self.started_at=started_at
                        if not self.store.get_state('autonomy_error',''):
                            self.store.set_state('autonomy_error','local_recovery_interrupted')
                        return
                    self.store.promote_completed_model_turn(previous)
                    recovered = (self.store.action_hold_valid(previous)
                                 or self.store.hold_pending_session_actions(previous)
                                 or self.store.recover_confirmed_session(previous))
                    if recovered:
                        self.active_session_id = previous
                        self.started_at = started_at
                        return
        interrupted = self.store.interrupt_unfinished_cycles()
        if previous:
            self.store.record_session_failure('interrupted', self.controller.owner_id, session_id=previous)
            self._record_incident(previous, "restart", started)
        elif interrupted:
            self.store.set_state("session_start_requested", "")
        if interrupted:
            self.store.add_activity(f"Interrupted {interrupted} unfinished autonomous cycle(s) during recovery")

    def _schedule_unsent_retry(self, error, session_id, cycle_id, now):
        recovery = getattr(self.agent, 'local_recovery', None)
        if (not isinstance(error, LettaTurnError) or error.safe_category != 'provider_not_reached'
                or (recovery is not None and getattr(recovery, 'enabled', False))):
            return False
        try:
            retry_at = self.store.schedule_unsent_provider_retry(session_id, cycle_id, self.controller.owner_id, now)
        except Exception:
            logging.warning('Unsent provider retry could not be scheduled; retaining failure handling')
            return False
        if retry_at is None:
            return False
        logging.warning('Provider unreachable before sending; autonomous retry scheduled')
        self.active_session_id = ''
        self.started_at = None
        return True

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
            "fallback_wake", "scheduled_wake", "agent_continue", "owner_wake", "conversation_quiet",
        }:
            return None
        if self.store.get_state("sleeping", "0") == "1" or self.store.get_state("paused", "0") == "1":
            return None
        session_id = uuid.uuid4().hex
        if not self.store.start_session_state(session_id, now.astimezone(timezone.utc).isoformat(timespec='seconds'), trigger):
            return None
        self.active_session_id = session_id
        self.started_at = now
        return self.active_session_id

    def stop(self, reason):
        if not self.active_session_id:
            return
        updates = {"active_session_id": "", "active_session_started_at": "", "active_session_status": reason}
        updates['session_start_requested'] = ''
        try:
            self.store.stop_session_state(self.active_session_id, updates, f"Autonomous session stopped ({reason})")
        finally:
            # Prevent another provider call in this process even if state storage
            # has failed; startup recovery will reconcile the persisted session.
            self.active_session_id = ""
            self.started_at = None

    def fail(self, category):
        try:
            self.store.record_session_failure(category, self.controller.owner_id, session_id=self.active_session_id)
        finally:
            self.active_session_id = ''
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
        if self.active_session_id and self.store.action_hold_valid(self.active_session_id):
            if not self.store.release_confirmed_action_hold(self.active_session_id):
                return False
        blocked = self.store.blocked_session_request()
        if blocked:
            self.stop(blocked)
            return False
        if (self.store.get_state('autonomy_error', '') or self.store.conversation_waiting(current)
                or self.store.pending_owner_input(self.controller.owner_id)):
            return False
        if (self.store.get_state('founding_letter_record_id', '')
                and self.store.get_state('founding_letter_delivered', '0') != '1'):
            return False
        if not self.active_session_id:
            revision = self.store.get_state('owner_control_revision', '0')
            trigger = self.store.get_state("session_start_requested", "")
            if not trigger:
                return False
            if trigger == 'provider_retry':
                session_id = uuid.uuid4().hex
                if not self.store.start_provider_retry_session(session_id, current, self.controller.owner_id):
                    return False
                self.active_session_id = session_id
                self.started_at = current
            elif not self.start(trigger, current):
                self.store.clear_session_request(trigger, revision)
                return False
        return self.run_cycle(self.active_session_id, current)

    def run_cycle(self, session_id, now=None):
        current = datetime.now(timezone.utc) if now is None else now
        if (self.store.get_state('autonomy_error', '') or self.store.conversation_waiting(current)
                or self.store.pending_owner_input(self.controller.owner_id)):
            return False
        if not self.active_session_id or session_id != self.active_session_id:
            return False
        if self.store.get_state('active_session_id', '') != session_id:
            self.active_session_id = ''
            self.started_at = None
            return False
        if self.store.get_state("sleeping", "0") == "1" or self.store.get_state("paused", "0") == "1":
            self.stop("lifecycle_stop")
            return False
        _, day = self.controller._periods(current)
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
            research_context = self.controller.research.wake_context()
            # The gateway checks this only between provider requests. Finish the
            # current request/tools, then yield without dispatching another one.
            self.store.set_state('autonomous_owner_id', str(self.controller.owner_id))
            try:
                recovery = self.store.get_state('session_recovery_checkpoint', '')
                recovery_context = ('\nController recovery receipt (data): ' + recovery
                                    + '\nContinue from the saved conversation and research. Do not repeat '
                                      'completed external actions from the prior cycle. This receipt does not '
                                      'authorize replaying held messages or uncertain actions.' if recovery else '')
                self.store.checkpoint_cycle_turn(cycle_id, 'model_started')
                self.agent.autonomous_turn(
                    self.SESSION_PROMPT + f" Today's diary is {diary_status}.\n" + research_context + recovery_context,
                    timeout=None,
                )
                self.store.checkpoint_cycle_turn(cycle_id, 'model_confirmed')
            finally:
                self.store.set_state('autonomous_owner_id', '')
            if self.store.get_state('active_session_id', '') != session_id:
                raise LettaTurnCancelled('Owner changed the active session')
            if self.store.hold_confirmed_actions(session_id, cycle_id):
                return False
            if self.store.cycle_uncertain_effects(cycle_id):
                raise LettaTurnError('action_outcome_uncertain')
            stage = "finish_cycle"
            self.store.finish_cycle(cycle_id, "complete")
            self.store.set_state('session_recovery_checkpoint', '')
        except RecoveryInterrupted as error:
            if (cycle_started and self.store.get_state('active_session_id','')==session_id
                    and self.store.get_state('autonomy_error','') in ('','local_recovery_interrupted')):
                self.store.set_state('autonomy_error','local_recovery_interrupted')
                self.store.add_activity('Journaled model turn held (category='+error.category+')')
            return False
        except LettaTurnYielded:
            if cycle_started:
                self.store.finish_cycle(cycle_id, 'stopped')
            self.store.add_activity('Autonomous turn yielded to waiting owner input')
            return False
        except LettaTurnCancelled as error:
            record_failure(error, 'cancelled')
            if cycle_started:
                self.store.finish_cycle(cycle_id, 'stopped')
            # Owner pause/wake already wrote the authoritative state. Do not
            # replace it with provider_error, sleep, or another session's state.
            if self.active_session_id == session_id:
                self.active_session_id = ''
                self.started_at = None
            return False
        except Exception as error:
            record_failure(error)
            if self.store.get_state('active_session_id', '') != session_id:
                if cycle_started:
                    self.store.finish_cycle(cycle_id, 'stopped')
                self.active_session_id = ''
                self.started_at = None
                return False
            try:
                if cycle_started and stage == 'agent_turn' and self.store.promote_completed_model_turn(session_id):
                    if (self.store.hold_confirmed_actions(session_id, cycle_id)
                            or self.store.recover_confirmed_session(session_id)):
                        return False
            except Exception:
                logging.warning('Completed-turn recovery could not be persisted; retaining failure handling')
            if cycle_started and stage == 'agent_turn' and self._schedule_unsent_retry(error, session_id, cycle_id, current):
                return False
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
            self.fail(category)
            return False
        try:
            cycles_today = self.store.cycle_count(day)
            self.store.add_activity(f"Autonomous cycle completed ({cycles_today} today)")
        except Exception as error:
            record_failure(error)
            self.fail('ledger_error')
            return False
        if self.store.get_state("sleeping", "0") == "1":
            self.stop("agent_sleep")
        elif self.store.get_state("paused", "0") == "1":
            self.stop("paused")
        return True
