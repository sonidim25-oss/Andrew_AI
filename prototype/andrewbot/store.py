import sqlite3
import threading
import json
import uuid
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps
from zoneinfo import ZoneInfo
from .latency import wall_age_seconds


def database_locked(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return invoke


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                queued INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS messages_owner_id ON messages(owner_id, id);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS updates (update_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS telegram_inbox (
                update_id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL,
                received_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_claims (
                update_id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS telegram_uncertain (
                update_id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL,
                received_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_outbox (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS telegram_delivery_progress (
                kind TEXT NOT NULL,
                record_id TEXT NOT NULL,
                content TEXT NOT NULL,
                next_chunk INTEGER NOT NULL DEFAULT 0,
                chunk_attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(kind,record_id)
            );
            CREATE TABLE IF NOT EXISTS usage (
                id INTEGER PRIMARY KEY,
                month TEXT NOT NULL,
                day TEXT NOT NULL,
                cost_usd REAL NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY,
                event TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS diary_entries (
                entry_id TEXT PRIMARY KEY,
                local_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                record_type TEXT NOT NULL CHECK(record_type IN ('diary','incident_summary')),
                delivery_state TEXT NOT NULL DEFAULT 'pending' CHECK(delivery_state IN ('pending','delivered','uncertain')),
                delivery_attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS diary_delivery_claims (
                entry_id TEXT PRIMARY KEY REFERENCES diary_entries(entry_id),
                token TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS diary_one_per_day
                ON diary_entries(local_date) WHERE record_type='diary';
            CREATE UNIQUE INDEX IF NOT EXISTS diary_one_incident_per_session
                ON diary_entries(session_id) WHERE record_type='incident_summary' AND session_id<>'';
            CREATE TABLE IF NOT EXISTS diary_event_refs (
                entry_id TEXT NOT NULL REFERENCES diary_entries(entry_id),
                activity_id INTEGER NOT NULL REFERENCES activity(id),
                PRIMARY KEY(entry_id, activity_id)
            );
            CREATE TABLE IF NOT EXISTS memory_records (
                record_id TEXT PRIMARY KEY,
                record_type TEXT NOT NULL CHECK(record_type IN ('experience','interest')),
                created_at TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS personality_revisions (
                revision_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                effective_date TEXT NOT NULL,
                content TEXT NOT NULL,
                reason TEXT NOT NULL,
                source TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_event_refs (
                record_id TEXT NOT NULL REFERENCES memory_records(record_id),
                activity_id INTEGER NOT NULL REFERENCES activity(id),
                PRIMARY KEY(record_id, activity_id)
            );
            CREATE TABLE IF NOT EXISTS memory_diary_refs (
                record_id TEXT NOT NULL REFERENCES memory_records(record_id),
                entry_id TEXT NOT NULL REFERENCES diary_entries(entry_id),
                PRIMARY KEY(record_id, entry_id)
            );
            CREATE TABLE IF NOT EXISTS personality_event_refs (
                revision_id TEXT NOT NULL REFERENCES personality_revisions(revision_id),
                activity_id INTEGER NOT NULL REFERENCES activity(id),
                PRIMARY KEY(revision_id, activity_id)
            );
            CREATE TABLE IF NOT EXISTS personality_diary_refs (
                revision_id TEXT NOT NULL REFERENCES personality_revisions(revision_id),
                entry_id TEXT NOT NULL REFERENCES diary_entries(entry_id),
                PRIMARY KEY(revision_id, entry_id)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                record_id UNINDEXED, record_type UNINDEXED, created_at UNINDEXED,
                content, source UNINDEXED, tokenize='unicode61'
            );
            CREATE TABLE IF NOT EXISTS cycles (
                cycle_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                local_day TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                reserved_cost_usd REAL NOT NULL DEFAULT 0,
                actual_cost_usd REAL
            );
            CREATE INDEX IF NOT EXISTS cycles_local_day ON cycles(local_day, status);
            CREATE TABLE IF NOT EXISTS budget_reservations (
                reservation_id TEXT PRIMARY KEY,
                month TEXT NOT NULL,
                day TEXT NOT NULL,
                cost_usd REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS budget_reservations_period ON budget_reservations(month, day);
            CREATE TABLE IF NOT EXISTS research_entries (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL UNIQUE,
                revision INTEGER NOT NULL DEFAULT 1,
                question TEXT NOT NULL,
                findings TEXT NOT NULL DEFAULT '',
                doubts TEXT NOT NULL DEFAULT '',
                next_step TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('open','paused','closed')),
                parent_id TEXT REFERENCES research_entries(record_id) ON DELETE SET NULL,
                sources TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                entry_id TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                cycle_id TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL,
                tool_name TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id TEXT REFERENCES research_entries(record_id) ON DELETE CASCADE,
                revision INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                stop_type TEXT NOT NULL,
                stop_note TEXT NOT NULL DEFAULT '',
                next_step TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        # Additive migration: historical enqueue times are unknown, not "now".
        columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(telegram_outbox)')}
        if 'created_at' not in columns:
            self.connection.execute('ALTER TABLE telegram_outbox ADD COLUMN created_at TEXT')
        for name, declaration in {'lane': "TEXT NOT NULL DEFAULT 'normal'",
                                  'delivery_state': "TEXT NOT NULL DEFAULT 'pending'",
                                  'claim_token': 'TEXT'}.items():
            if name not in columns:
                self.connection.execute('ALTER TABLE telegram_outbox ADD COLUMN ' + name + ' ' + declaration)
        progress_columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(telegram_delivery_progress)')}
        if 'retry_notice' not in progress_columns:
            self.connection.execute('ALTER TABLE telegram_delivery_progress ADD COLUMN retry_notice INTEGER NOT NULL DEFAULT 0')
        self.connection.commit()
        try:
            self.path.chmod(0o600)
        except FileNotFoundError:
            pass

    @database_locked
    def close(self):
        self.connection.close()

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _research_event(self, kind, entry_id='', tool_name='', outcome='', session_id=None, cycle_id=None):
        """Caller holds the database lock and transaction; never store tool arguments."""
        cycle_id = self.get_state('active_cycle_id', '') if cycle_id is None else cycle_id
        origin = ('owner' if kind.startswith('owner_') else
                  'controller' if kind in {'session_started', 'technical_failure', 'time_limit', 'cycle_limit'} else
                  'autonomous' if cycle_id else 'interactive')
        self.connection.execute(
            'INSERT INTO research_events(kind,entry_id,session_id,cycle_id,origin,tool_name,outcome,created_at) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (kind, entry_id, self.get_state('active_session_id', '') if session_id is None else session_id, cycle_id,
             origin, tool_name, outcome, self.now()),
        )

    @database_locked
    def research_tool_context(self):
        return (self.get_state('active_session_id', ''), self.get_state('active_cycle_id', ''), self._focused_research_id())

    @database_locked
    def record_research_tool(self, name, outcome, context):
        session_id, cycle_id, entry_id = context
        if self.get_state('active_session_id', '') == session_id:
            entry_id = self._focused_research_id()
        with self.connection:
            self._research_event('tool_used', entry_id, name, outcome, session_id=session_id, cycle_id=cycle_id)

    def _focused_research_id(self):
        session = self.get_state('active_session_id', '')
        if self.get_state('research_focus_session', '') != session:
            return ''
        return self.get_state('research_focus_id', '')

    def _research_checkpoint(self, stop_type, stop_note=''):
        entry_id = self._focused_research_id()
        self._research_event(stop_type, entry_id)
        entry = self.research_entry(entry_id) if entry_id else None
        self.connection.execute(
            'INSERT INTO research_checkpoints(entry_id,revision,session_id,cycle_id,stop_type,stop_note,next_step,created_at) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (entry_id if entry else None, entry['revision'] if entry else 0, self.get_state('active_session_id', ''),
             self.get_state('active_cycle_id', ''), stop_type, stop_note, entry['next_step'] if entry else '', self.now()),
        )

    @database_locked
    def research_last_checkpoint(self, record_id):
        row = self.connection.execute('SELECT * FROM research_checkpoints WHERE entry_id=? ORDER BY id DESC LIMIT 1', (record_id,)).fetchone()
        return dict(row) if row else None

    @database_locked
    def recent_research_observations(self, limit=20):
        return [dict(row) for row in reversed(self.connection.execute(
            'SELECT * FROM research_events ORDER BY id DESC LIMIT ?', (limit,)).fetchall())]

    @database_locked
    def research_entry(self, record_id):
        row = self.connection.execute('SELECT * FROM research_entries WHERE record_id=?', (record_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['sources'] = json.loads(result['sources'])
        return result

    @database_locked
    def research_page(self, status='unfinished', after=0, limit=10):
        condition = "status IN ('open','paused')" if status == 'unfinished' else 'status=?' if status != 'all' else '1=1'
        args = [] if status in {'unfinished', 'all'} else [status]
        rows = self.connection.execute(
            f'SELECT record_id,seq FROM research_entries WHERE {condition} AND seq>? ORDER BY seq LIMIT ?',
            [*args, after, limit + 1],
        ).fetchall()
        return {'entries': [self.research_entry(row['record_id']) for row in rows[:limit]],
                'next_after': rows[limit - 1]['seq'] if len(rows) > limit else None}

    @database_locked
    def save_research_entry(self, fields, record_id=None, revision=None):
        self._research_mutation_allowed()
        allowed = {'question', 'findings', 'doubts', 'next_step', 'status', 'parent_id', 'sources'}
        if set(fields) - allowed:
            raise ValueError('unsupported research fields')
        current = self.research_entry(record_id) if record_id else None
        if record_id and (current is None or current['revision'] != revision):
            raise ValueError('research entry is missing or changed; read it again before editing')
        if not record_id and self.connection.execute('SELECT count(*) FROM research_entries').fetchone()[0] >= 500:
            raise ValueError('research journal capacity reached; edit or delete existing entries')
        if fields.get('parent_id') and (fields['parent_id'] == record_id or not self.research_entry(fields['parent_id'])):
            raise ValueError('parent question must identify another existing research entry')
        parent_id = fields.get('parent_id')
        while parent_id:
            if parent_id == record_id:
                raise ValueError('parent questions cannot form a cycle')
            parent_id = self.research_entry(parent_id)['parent_id']
        combined = {**(current or {}), **fields}
        if len(json.dumps(combined, ensure_ascii=False).encode('utf-8')) > 20_000:
            raise ValueError('research entry exceeds its total byte limit')
        now = self.now()
        with self.connection:
            if current:
                assignments = ','.join(f'{key}=?' for key in fields)
                values = [json.dumps(value, ensure_ascii=False) if key == 'sources' else value for key, value in fields.items()]
                self.connection.execute(f'UPDATE research_entries SET {assignments},revision=revision+1,updated_at=? WHERE record_id=?',
                                        [*values, now, record_id])
                kind = 'question_updated'
            else:
                record_id = uuid.uuid4().hex
                self.connection.execute(
                    'INSERT INTO research_entries(record_id,question,findings,doubts,next_step,status,parent_id,sources,created_at,updated_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (record_id, fields['question'], fields.get('findings', ''), fields.get('doubts', ''),
                     fields.get('next_step', ''), fields.get('status', 'open'), fields.get('parent_id'),
                     json.dumps(fields.get('sources', []), ensure_ascii=False), now, now),
                )
                kind = 'question_created'
            self._research_event(kind, record_id, outcome=fields.get('status', current['status'] if current else 'open'))
            if fields.get('status') == 'closed' and self.get_state('research_focus_id', '') == record_id:
                self.connection.execute("DELETE FROM state WHERE key IN ('research_focus_id','research_focus_session')")
        return self.research_entry(record_id)

    @database_locked
    def focus_research_entry(self, record_id):
        self._research_mutation_allowed()
        entry = self.research_entry(record_id)
        if entry is None or entry['status'] == 'closed':
            raise ValueError('select an existing open or paused question')
        previous = self.connection.execute(
            "SELECT 1 FROM research_events WHERE entry_id=? AND kind IN ('topic_selected','topic_resumed') LIMIT 1", (record_id,)
        ).fetchone()
        with self.connection:
            for key, value in {'research_focus_id': record_id,
                               'research_focus_session': self.get_state('active_session_id', '')}.items():
                self.connection.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
            self._research_event('topic_resumed' if previous else 'topic_selected', record_id)
        return entry

    @database_locked
    def delete_research_entry(self, record_id, revision):
        self._research_mutation_allowed()
        with self.connection:
            self.connection.execute('UPDATE research_entries SET parent_id=NULL,revision=revision+1,updated_at=? WHERE parent_id=?',
                                    (self.now(), record_id))
            deleted = self.connection.execute('DELETE FROM research_entries WHERE record_id=? AND revision=?', (record_id, revision)).rowcount
            if not deleted:
                raise ValueError('research entry is missing or changed; read it again before deleting')
            if self.get_state('research_focus_id', '') == record_id:
                self.connection.execute("DELETE FROM state WHERE key IN ('research_focus_id','research_focus_session')")
            self._research_event('question_deleted', record_id)
        return True

    def _research_mutation_allowed(self):
        if (self.get_state('paused', '0') == '1' or self.get_state('sleeping', '0') == '1'
                or self.get_state('autonomy_error', '')):
            raise ValueError('research journal changes cannot override owner controls or a stopped session')

    @database_locked
    def research_observations(self, after=0, limit=50):
        return [dict(row) for row in self.connection.execute(
            'SELECT * FROM research_events WHERE id>? ORDER BY id LIMIT ?', (after, limit)
        ).fetchall()]

    @database_locked
    def add_message(self, owner_id, role, content, queued=False):
        cursor = self.connection.execute(
            "INSERT INTO messages(owner_id, role, content, created_at, queued) VALUES(?,?,?,?,?)",
            (owner_id, role, content, self.now(), int(queued)),
        )
        self.connection.commit()
        return cursor.lastrowid

    @database_locked
    def recent_messages(self, owner_id, limit=12):
        rows = self.connection.execute(
            "SELECT role, content, created_at, queued FROM messages WHERE owner_id=? AND queued=0 ORDER BY id DESC LIMIT ?",
            (owner_id, limit),
        ).fetchall()
        return list(reversed(rows))

    @database_locked
    def pending_message_count(self, owner_id):
        row = self.connection.execute(
            "SELECT COUNT(*) FROM messages WHERE owner_id=? AND role='user' AND queued IN (1,2,3)", (owner_id,)
        ).fetchone()
        return row[0]

    @database_locked
    def uncertain_message_count(self, owner_id):
        row = self.connection.execute(
            "SELECT COUNT(*) FROM messages WHERE owner_id=? AND role='user' AND queued=3", (owner_id,)
        ).fetchone()
        return int(row[0])

    @database_locked
    def queued_messages(self, owner_id):
        rows = self.connection.execute(
            "SELECT id, content, created_at FROM messages WHERE owner_id=? AND role='user' AND queued=1 ORDER BY id",
            (owner_id,),
        ).fetchall()
        return rows

    @database_locked
    def claim_owner_message(self, owner_id, message_id):
        with self.connection:
            return self.connection.execute(
                "UPDATE messages SET queued=2 WHERE id=? AND owner_id=? AND role='user' AND queued=1",
                (message_id, owner_id),
            ).rowcount == 1

    @database_locked
    def claim_queued_messages(self, owner_id, max_bytes=40_000):
        with self.connection:
            rows = self.connection.execute(
                "SELECT id,content,created_at FROM messages WHERE owner_id=? AND role='user' AND queued=1 ORDER BY id LIMIT 50",
                (owner_id,),
            ).fetchall()
            # Claim only complete messages that fit one model input. Later
            # batches stay queued; truncating a claimed batch would lose mail.
            batch, used = [], 0
            for row in rows:
                size = len(row['content'].encode('utf-8')) + 1
                if used + size > max_bytes:
                    break
                batch.append(row)
                used += size
            rows = batch
            if rows:
                ids = [row["id"] for row in rows]
                marks = ",".join("?" for _ in ids)
                self.connection.execute(
                    f"UPDATE messages SET queued=2 WHERE id IN ({marks})", ids
                )
            return rows

    @database_locked
    def release_queued(self, owner_id, message_ids=None):
        if message_ids is None:
            self.connection.execute("UPDATE messages SET queued=0 WHERE owner_id=? AND queued IN (1,2)", (owner_id,))
        elif message_ids:
            marks = ",".join("?" for _ in message_ids)
            self.connection.execute(
                f"UPDATE messages SET queued=0 WHERE owner_id=? AND queued=2 AND id IN ({marks})",
                [owner_id, *message_ids],
            )
        self.connection.commit()

    @database_locked
    def restore_queued_claim(self, message_ids):
        if not message_ids:
            return
        marks = ",".join("?" for _ in message_ids)
        self.connection.execute(
            f"UPDATE messages SET queued=1 WHERE queued=2 AND id IN ({marks})", list(message_ids)
        )
        self.connection.commit()

    @database_locked
    def hold_uncertain_queued_claim(self, message_ids):
        if not message_ids:
            return
        marks = ",".join("?" for _ in message_ids)
        self.connection.execute(
            f"UPDATE messages SET queued=3 WHERE queued=2 AND id IN ({marks})", list(message_ids)
        )
        self.connection.commit()

    @database_locked
    def recover_queued_claims(self):
        with self.connection:
            cursor = self.connection.execute("UPDATE messages SET queued=3 WHERE queued=2")
            if cursor.rowcount:
                self.connection.execute(
                    "INSERT INTO activity(event,created_at) VALUES(?,?)",
                    (f"Held {cursor.rowcount} queued message(s) with uncertain prior delivery", self.now()),
                )
            return cursor.rowcount

    @database_locked
    def retry_uncertain_messages(self, owner_id):
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE messages SET queued=1 WHERE owner_id=? AND role='user' AND queued=3", (owner_id,)
            )
            if cursor.rowcount:
                # Only explicit owner replay authorizes another attempt at a
                # previously uncertain payload. Existing reservations stay held.
                self.connection.execute(
                    "INSERT INTO state(key,value) VALUES('provider_retry_revision',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (uuid.uuid4().hex,))
            return cursor.rowcount

    @database_locked
    def get_state(self, key, default=None):
        row = self.connection.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @database_locked
    def set_state(self, key, value):
        self.connection.execute(
            "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.connection.commit()

    @database_locked
    def transition_state(self, updates, event, chat_id=None, notice=None, stop_note=''):
        if not isinstance(updates, dict) or not isinstance(event, str) or not event:
            raise ValueError("state transition requires updates and an event")
        with self.connection:
            stop_type = ''
            if str(updates.get('sleeping', '')) == '1':
                stop_type = 'voluntary_sleep' if event.startswith('Andrew ') else 'owner_sleep'
            elif str(updates.get('paused', '')) == '1':
                stop_type = 'owner_pause'
            elif updates.get('active_session_status') == 'error':
                stop_type = 'technical_failure'
            elif updates.get('active_session_status') in {'time_limit', 'cycle_limit', 'interrupted', 'owner_wake'}:
                stop_type = updates['active_session_status']
            if stop_type:
                self._research_checkpoint(stop_type, stop_note)
            for key, value in updates.items():
                self.connection.execute(
                    "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(key), str(value)),
                )
            self.connection.execute(
                "INSERT INTO activity(event,created_at) VALUES(?,?)", (event, self.now())
            )
            if notice and chat_id is not None:
                self.connection.execute("INSERT INTO telegram_outbox(chat_id,text,created_at) VALUES(?,?,?)", (chat_id, notice, self.now()))
            if updates.get('active_session_id'):
                self._research_event('session_started')
            if updates.get('active_session_id') == '':
                self.connection.execute("DELETE FROM state WHERE key='active_cycle_id'")

    @database_locked
    def conversation_waiting(self, now):
        until = self.get_state('conversation_until', '')
        return bool(until and datetime.fromisoformat(until) > now.astimezone(timezone.utc))

    @database_locked
    def note_conversation(self, until, enabled, owner_revision, completed=False):
        # A late reply must never undo an owner command issued during the turn.
        if self.get_state('owner_control_revision', '0') != owner_revision:
            return False
        if self.get_state('paused', '0') == '1' or self.get_state('sleeping', '0') == '1':
            return False
        updates = {'conversation_until': until}
        if completed:
            updates['autonomy_error'] = ''
            if not self.get_state('active_session_id', ''):
                updates['active_session_status'] = 'waiting'
        if enabled and (completed or not self.get_state('autonomy_error', '')):
            updates['session_start_requested'] = 'conversation_quiet'
        self.transition_state(updates, 'Owner conversation waiting for a follow-up')
        return True

    @database_locked
    def pending_owner_input(self, owner_id):
        # Search the entire durable inbox, including messages beyond its first page.
        return self.connection.execute(
            "SELECT 1 FROM telegram_inbox WHERE json_extract(payload,'$.message.from.id')=? "
            "AND json_extract(payload,'$.message.chat.id')=? "
            "AND json_extract(payload,'$.message.chat.type')='private' "
            "AND json_type(payload,'$.message.text')='text' "
            "AND trim(json_extract(payload,'$.message.text'))<>'' "
            "AND json_type(payload,'$.message.forward_origin') IS NULL "
            "AND json_type(payload,'$.message.forward_from') IS NULL "
            "AND json_type(payload,'$.message.forward_from_chat') IS NULL "
            "AND json_type(payload,'$.message.forward_sender_name') IS NULL "
            "AND update_id NOT IN (SELECT update_id FROM telegram_claims) LIMIT 1",
            (owner_id, owner_id),
        ).fetchone() is not None or self.connection.execute(
            "SELECT 1 FROM messages WHERE owner_id=? AND role='user' AND queued IN (1,2) LIMIT 1",
            (owner_id,),
        ).fetchone() is not None

    @database_locked
    def record_session_failure(self, category, owner_id, owner_revision=None, session_id=None, notify=True):
        if owner_revision is not None and self.get_state('owner_control_revision', '0') != owner_revision:
            return False
        if session_id is not None and self.get_state('active_session_id', '') != session_id:
            return False
        self.transition_state(
            {'active_session_id': '', 'active_session_started_at': '', 'active_session_status': 'error',
             'autonomy_error': category, 'session_start_requested': ''},
            f'Session failed (category={category}); automatic model calls stopped',
            owner_id, self.failure_notice(category) if notify else None,
        )
        return True

    @staticmethod
    def failure_notice(category):
        return (f'My session failed (error: {category}); your messages are saved. '
                'I have stopped automatic model calls. Use /wake to resume; uncertain messages '
                'will stay held unless you explicitly use /replay. Retrying could repeat completed actions.')

    @database_locked
    def choose_sleep(self, updates, event, owner_id, notice, now, stop_note=''):
        if (self.get_state('paused', '0') == '1' or self.get_state('sleeping', '0') == '1'
                or self.get_state('autonomy_error', '') or self.conversation_waiting(now)
                or self.pending_owner_input(owner_id)):
            raise ValueError('Stay available for owner messages; sleep is unavailable during conversation or an error')
        self.transition_state(updates, event, owner_id, notice, stop_note)

    @database_locked
    def continue_activity_state(self):
        if (self.get_state('paused', '0') == '1' or self.get_state('sleeping', '0') == '1'
                or self.get_state('autonomy_error', '')):
            raise ValueError('Owner controls and failed sessions cannot be overridden by an agent')
        self.transition_state({'session_start_requested': 'agent_continue'}, 'Andrew continued activity')

    @database_locked
    def scheduled_wake_state(self, current, owner_id):
        if (self.get_state('paused', '0') == '1' or self.get_state('autonomy_error', '')
                or self.get_state('sleeping', '0') != '1' or not self.get_state('sleep_until', '')):
            return False
        due = datetime.fromisoformat(self.get_state('sleep_until'))
        if due.tzinfo is None:
            raise ValueError('stored wake time must be timezone-aware')
        if current.astimezone(timezone.utc) < due.astimezone(timezone.utc):
            return False
        pending = self.pending_message_count(owner_id)
        self.transition_state({'sleeping': '0', 'sleep_until': '', 'session_start_requested': 'scheduled_wake'},
                              f'Scheduled wake completed; {pending} queued message(s) pending')
        return True

    @database_locked
    def start_session_state(self, session_id, started_at, trigger):
        if (self.get_state('paused', '0') == '1' or self.get_state('sleeping', '0') == '1'
                or self.get_state('autonomy_error', '')):
            return False
        self.transition_state({'active_session_id': session_id, 'active_session_started_at': started_at,
                               'active_session_status': 'running', 'session_start_requested': ''},
                              f'Autonomous session started ({trigger})')
        return True

    @database_locked
    def transition_owner_control(self, updates, event):
        revision = int(self.get_state('owner_control_revision', '0')) + 1
        self.transition_state({**updates, 'owner_control_revision': str(revision)}, event)

    @database_locked
    def blocked_session_request(self):
        reason = ('paused' if self.get_state('paused', '0') == '1' else
                  'sleeping' if self.get_state('sleeping', '0') == '1' else '')
        if reason:
            self.set_state('session_start_requested', '')
        return reason

    @database_locked
    def clear_session_request(self, trigger, owner_revision):
        if (self.get_state('session_start_requested', '') == trigger
                and self.get_state('owner_control_revision', '0') == owner_revision):
            self.set_state('session_start_requested', '')

    @database_locked
    def stop_session_state(self, session_id, updates, event):
        if self.get_state('active_session_id', '') != session_id:
            return False
        self.transition_state(updates, event)
        return True

    @database_locked
    def process_fallback_wake(self, local_day, owner_id):
        """Start one daily session from idle or indefinite sleep; respect timed sleep and pause."""
        with self.connection:
            state = dict(self.connection.execute(
                "SELECT key,value FROM state WHERE key IN ('sleeping','sleep_until','fallback_wake_day',"
                "'paused','active_session_id','session_start_requested','autonomy_error')"
            ).fetchall())
            if state.get("paused", "0") == "1":
                return False
            if state.get("fallback_wake_day", "") >= local_day:
                return False
            eligible = (not state.get('autonomy_error', '') and not state.get("active_session_id", "") and not state.get("session_start_requested", "")
                        and not (state.get("sleeping", "0") == "1" and state.get("sleep_until", "")))
            updates = {"fallback_wake_day": local_day}
            if eligible:
                updates.update({"sleeping": "0", "sleep_until": "", "session_start_requested": "fallback_wake"})
            for key, value in updates.items():
                self.connection.execute(
                    "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            if eligible:
                pending = self.connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE owner_id=? AND role='user' AND queued IN (1,2,3)", (owner_id,)
                ).fetchone()[0]
                event = f"Daily fallback wake completed; {pending} queued message(s) pending"
            else:
                event = "Daily fallback checkpoint passed; existing lifecycle state preserved"
            self.connection.execute(
                "INSERT INTO activity(event,created_at) VALUES(?,?)",
                (event, self.now()),
            )
            return eligible

    @database_locked
    def begin_cycle(self, session_id, cycle_id, local_day, reserved_cost_usd=0.0):
        with self.connection:
            self.connection.execute(
                "INSERT INTO cycles(cycle_id,session_id,local_day,status,started_at,reserved_cost_usd) VALUES(?,?,?,?,?,?)",
                (cycle_id, session_id, local_day, "running", self.now(), max(0.0, float(reserved_cost_usd))),
            )
            self.connection.execute("INSERT INTO state(key,value) VALUES('active_cycle_id',?) "
                                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (cycle_id,))
            self._research_event('cycle_started', self._focused_research_id(), session_id=session_id, cycle_id=cycle_id)

    @database_locked
    def finish_cycle(self, cycle_id, status, actual_cost_usd=None):
        with self.connection:
            if status not in {"complete", "failed", "interrupted", "stopped"}:
                raise ValueError("invalid cycle status")
            cursor = self.connection.execute(
                "UPDATE cycles SET status=?,ended_at=?,actual_cost_usd=? WHERE cycle_id=? AND status='running'",
                (status, self.now(), None if actual_cost_usd is None else max(0.0, float(actual_cost_usd)), cycle_id),
            )
            if cursor.rowcount:
                session = self.connection.execute('SELECT session_id FROM cycles WHERE cycle_id=?', (cycle_id,)).fetchone()[0]
                self._research_event('cycle_finished', outcome=status, session_id=session, cycle_id=cycle_id)
                if status in {'complete', 'stopped'} and self.get_state('active_cycle_id', '') == cycle_id:
                    self.connection.execute("DELETE FROM state WHERE key='active_cycle_id'")
            return cursor.rowcount == 1

    @database_locked
    def cycle_count(self, local_day):
        row = self.connection.execute(
            "SELECT COUNT(*) FROM cycles WHERE local_day=?", (local_day,)
        ).fetchone()
        return int(row[0])

    @database_locked
    def interrupt_unfinished_cycles(self):
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE cycles SET status='interrupted',ended_at=? WHERE status='running'", (self.now(),)
            )
            return cursor.rowcount

    @database_locked
    def mark_update(self, update_id):
        try:
            self.connection.execute("INSERT INTO updates(update_id) VALUES(?)", (update_id,))
            self.connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    @database_locked
    def enqueue_update(self, update):
        update_id = update.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int):
            raise ValueError("Telegram update requires an integer update ID")
        with self.connection:
            if self.connection.execute("SELECT 1 FROM updates WHERE update_id=?", (update_id,)).fetchone():
                return False
            self.connection.execute(
                "INSERT OR IGNORE INTO telegram_inbox(update_id,payload,received_at) VALUES(?,?,?)",
                (update_id, json.dumps(update, separators=(",", ":")), self.now()),
            )
            return self.connection.execute(
                "SELECT 1 FROM telegram_inbox WHERE update_id=?", (update_id,)
            ).fetchone() is not None

    @database_locked
    def pending_updates(self):
        rows = self.connection.execute(
            "SELECT update_id,payload FROM telegram_inbox ORDER BY update_id LIMIT 50"
        ).fetchall()
        return [(row["update_id"], json.loads(row["payload"])) for row in rows]

    @database_locked
    def pending_owner_controls(self, owner_id, buttons=(), model_commands=()):
        text = "lower(trim(COALESCE(json_extract(payload,'$.message.text'),'')))"
        clauses, values = [f"{text} GLOB '/*'"], [owner_id, owner_id]
        for button in buttons:
            clauses.append(f'{text}=?')
            values.append(button.lower())
        if self.get_state('telegram_wake_prompt_until', ''):
            clauses.append('update_id>?')
            values.append(int(self.get_state('telegram_wake_prompt_after_update_id', '-1')))
        exclusion = ''
        if model_commands:
            for whitespace in (9, 10, 11, 12, 13):
                text = f"replace({text},char({whitespace}),' ')"
            text = f'trim({text})'
            command = f"substr({text},1,instr({text} || ' ',' ')-1)"
            base_command = f"substr({command},1,instr({command} || '@','@')-1)"
            exclusion = f"AND {base_command} NOT IN ({','.join('?' for _ in model_commands)}) "
            values.extend(model_commands)
        rows = self.connection.execute(
            "SELECT update_id,payload FROM telegram_inbox WHERE "
            "json_extract(payload,'$.message.from.id')=? AND json_extract(payload,'$.message.chat.id')=? "
            "AND json_extract(payload,'$.message.chat.type')='private' "
            "AND json_type(payload,'$.message.forward_origin') IS NULL "
            "AND json_type(payload,'$.message.forward_from') IS NULL "
            "AND json_type(payload,'$.message.forward_from_chat') IS NULL "
            "AND json_type(payload,'$.message.forward_sender_name') IS NULL "
            "AND json_type(payload,'$.message.text')='text' "
            "AND update_id NOT IN (SELECT update_id FROM telegram_claims) AND (" + ' OR '.join(clauses) + ') ' +
            exclusion + "ORDER BY update_id LIMIT 50", values).fetchall()
        return [(row['update_id'], json.loads(row['payload'])) for row in rows]

    @database_locked
    def complete_update(self, update_id, chat_id=None, reply=None, uncertain=False, lane='normal'):
        self._validate_lane(lane)
        with self.connection:
            if uncertain:
                self.connection.execute('INSERT OR IGNORE INTO telegram_uncertain SELECT * FROM telegram_inbox WHERE update_id=?', (update_id,))
            if reply and chat_id is not None:
                self.connection.execute("INSERT INTO telegram_outbox(chat_id,text,created_at,lane) VALUES(?,?,?,?)", (chat_id, reply, self.now(), lane))
            self.connection.execute("INSERT OR IGNORE INTO updates(update_id) VALUES(?)", (update_id,))
            self.connection.execute("DELETE FROM telegram_inbox WHERE update_id=?", (update_id,))
            self.connection.execute("DELETE FROM telegram_claims WHERE update_id=?", (update_id,))

    @database_locked
    def claim_update(self, update_id):
        with self.connection:
            return self.connection.execute(
                "INSERT OR IGNORE INTO telegram_claims(update_id) "
                "SELECT update_id FROM telegram_inbox WHERE update_id=?", (update_id,)
            ).rowcount == 1

    @database_locked
    def recover_update_claims(self, owner_id):
        # A crash after an external action has an unknown outcome. Never
        # automatically rerun the action, even if its reply wasn't saved.
        with self.connection:
            rows = self.connection.execute("SELECT update_id FROM telegram_claims").fetchall()
            for row in rows:
                self.connection.execute('INSERT OR IGNORE INTO telegram_uncertain SELECT * FROM telegram_inbox WHERE update_id=?', (row[0],))
                self.connection.execute("INSERT OR IGNORE INTO updates(update_id) VALUES(?)", (row[0],))
                self.connection.execute("DELETE FROM telegram_inbox WHERE update_id=?", (row[0],))
            self.connection.execute("DELETE FROM telegram_claims")
            if rows:
                self.connection.execute("INSERT INTO telegram_outbox(chat_id,text,created_at) VALUES(?,?,?)", (
                    owner_id, "Andrew restarted during an owner request. Its outcome is uncertain; it was not automatically repeated. Please check the result before asking again.", self.now()))
            return len(rows)

    @database_locked
    def queue_reply(self, chat_id, text, lane='normal'):
        self._validate_lane(lane)
        if text:
            with self.connection:
                self.connection.execute("INSERT INTO telegram_outbox(chat_id,text,created_at,lane) VALUES(?,?,?,?)", (chat_id, text, self.now(), lane))

    @database_locked
    def inbox_received_at(self, update_id):
        row = self.connection.execute('SELECT received_at FROM telegram_inbox WHERE update_id=?',
                                      (update_id,)).fetchone()
        return row['received_at'] if row else None

    @database_locked
    def reply_age_seconds(self, reply_id, now):
        row = self.connection.execute('SELECT created_at FROM telegram_outbox WHERE id=?',
                                      (reply_id,)).fetchone()
        return wall_age_seconds(row['created_at'], now) if row else None

    @database_locked
    def next_reply(self):
        return self.connection.execute("SELECT * FROM telegram_outbox ORDER BY id LIMIT 1").fetchone()

    @staticmethod
    def _validate_lane(lane):
        if lane not in ('normal', 'system'):
            raise ValueError('Unknown reply lane')

    def _reply_status(self, row):
        # Caller holds the Store lock. Per-part acknowledgements override the
        # legacy attempt count; no progress after an attempt is uncertain.
        from .telegram import split_message
        progress = self.connection.execute(
            "SELECT * FROM telegram_delivery_progress WHERE kind='reply' AND record_id=?",
            (str(row['id']),)).fetchone()
        if progress is None:
            return 'held' if row['attempts'] else 'pending'
        if progress['chunk_attempts']:
            return 'held'
        if progress['next_chunk'] >= len(split_message(progress['content'])):
            return 'complete'
        return 'pending'

    def _delete_reply_progress(self, reply_id):
        self.connection.execute("DELETE FROM telegram_delivery_progress WHERE kind='reply' AND record_id=?", (str(reply_id),))

    @database_locked
    def claim_reply(self, lane):
        self._validate_lane(lane)
        # A single write statement arbitrates across independent connections.
        with self.connection:
            while True:
                token = uuid.uuid4().hex
                rows = self.connection.execute(
                    "UPDATE telegram_outbox SET delivery_state='sending',claim_token=? "
                    "WHERE id=(SELECT id FROM telegram_outbox WHERE lane=? AND delivery_state='pending' ORDER BY id LIMIT 1) "
                    "AND delivery_state='pending' AND NOT EXISTS "
                    "(SELECT 1 FROM telegram_outbox WHERE lane=? AND delivery_state='sending') RETURNING *",
                    (token, lane, lane)).fetchall()
                if not rows:
                    return None
                row = rows[0]
                status = self._reply_status(row)
                if status == 'complete':
                    self.connection.execute('DELETE FROM telegram_outbox WHERE id=?', (row['id'],))
                    self._delete_reply_progress(row['id'])
                elif status == 'held':
                    self.connection.execute("UPDATE telegram_outbox SET delivery_state='held',claim_token=NULL WHERE id=?", (row['id'],))
                else:
                    return dict(row)

    @database_locked
    def hold_reply(self, reply_id, token):
        with self.connection:
            return self.connection.execute(
                "UPDATE telegram_outbox SET delivery_state='held',claim_token=NULL "
                "WHERE id=? AND claim_token=? AND delivery_state='sending'", (reply_id, token)).rowcount == 1

    @database_locked
    def finish_reply(self, reply_id, token):
        with self.connection:
            row = self.connection.execute("SELECT * FROM telegram_outbox WHERE id=? AND claim_token=? AND delivery_state='sending'",
                                          (reply_id, token)).fetchone()
            if row is None or self._reply_status(row) != 'complete':
                return False
            removed = self.connection.execute("DELETE FROM telegram_outbox WHERE id=? AND claim_token=? AND delivery_state='sending'",
                                              (reply_id, token)).rowcount == 1
            if removed:
                self._delete_reply_progress(reply_id)
            return removed

    @database_locked
    def release_reply(self, reply_id, token):
        # Used on shutdown before the next part, never to permit an uncertain retry.
        with self.connection:
            row = self.connection.execute("SELECT * FROM telegram_outbox WHERE id=? AND claim_token=? AND delivery_state='sending'",
                                          (reply_id, token)).fetchone()
            if row is None:
                return False
            status = self._reply_status(row)
            if status == 'complete':
                return self.finish_reply(reply_id, token)
            return self.connection.execute('UPDATE telegram_outbox SET delivery_state=?,claim_token=NULL WHERE id=? AND claim_token=?',
                                           (status, reply_id, token)).rowcount == 1

    @database_locked
    def recover_reply_claims(self):
        """Startup only, before any senders: retain uncertainty, release unsent work."""
        recovered = 0
        with self.connection:
            for row in self.connection.execute("SELECT * FROM telegram_outbox").fetchall():
                status = self._reply_status(row)
                if status == 'complete':
                    self.connection.execute('DELETE FROM telegram_outbox WHERE id=?', (row['id'],))
                    self._delete_reply_progress(row['id'])
                    recovered += 1
                elif row['delivery_state'] == 'held':
                    continue
                elif row['delivery_state'] != status or row['claim_token'] is not None:
                    self.connection.execute('UPDATE telegram_outbox SET delivery_state=?,claim_token=NULL WHERE id=?', (status, row['id']))
                    recovered += 1
        return recovered

    @database_locked
    def attempt_reply(self, reply_id):
        with self.connection:
            self.connection.execute("UPDATE telegram_outbox SET attempts=attempts+1 WHERE id=?", (reply_id,))

    @database_locked
    def delivered_reply(self, reply_id):
        with self.connection:
            self.connection.execute("DELETE FROM telegram_outbox WHERE id=?", (reply_id,))
            self.connection.execute("DELETE FROM telegram_delivery_progress WHERE kind='reply' AND record_id=?", (str(reply_id),))

    @database_locked
    def prepare_telegram_delivery(self, kind, record_id, content, previously_attempted=False):
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO telegram_delivery_progress(kind,record_id,content,chunk_attempts) VALUES(?,?,?,?)",
                (kind, str(record_id), content, int(previously_attempted)))
            return dict(self.connection.execute(
                "SELECT * FROM telegram_delivery_progress WHERE kind=? AND record_id=?",
                (kind, str(record_id))).fetchone())

    @database_locked
    def attempt_telegram_chunk(self, kind, record_id, chunk):
        with self.connection:
            rows = self.connection.execute(
                "UPDATE telegram_delivery_progress SET chunk_attempts=1 "
                "WHERE kind=? AND record_id=? AND next_chunk=? AND chunk_attempts=0 RETURNING retry_notice",
                (kind, str(record_id), chunk)).fetchall()
            if not rows:
                return None
            if kind == 'reply':
                self.connection.execute('UPDATE telegram_outbox SET attempts=attempts+1 WHERE id=?', (record_id,))
            return rows[0]['retry_notice']

    @database_locked
    def acknowledge_telegram_chunk(self, kind, record_id, chunk):
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE telegram_delivery_progress SET next_chunk=next_chunk+1,chunk_attempts=0,retry_notice=0 "
                "WHERE kind=? AND record_id=? AND next_chunk=?",
                (kind, str(record_id), chunk))
            if cursor.rowcount != 1:
                raise ValueError('Telegram delivery progress changed')

    @database_locked
    def last_update_id(self):
        row = self.connection.execute(
            "SELECT MAX(update_id) FROM (SELECT update_id FROM updates UNION ALL SELECT update_id FROM telegram_inbox)"
        ).fetchone()
        return row[0]

    @database_locked
    def record_usage(self, month, day, cost_usd, input_tokens, output_tokens):
        self.connection.execute(
            "INSERT INTO usage(month,day,cost_usd,input_tokens,output_tokens,created_at) VALUES(?,?,?,?,?,?)",
            (month, day, cost_usd, input_tokens, output_tokens, self.now()),
        )
        self.connection.commit()

    @database_locked
    def reserve_usage(self, reservation_id, month, day, cost_usd):
        cursor = self.connection.execute(
            "INSERT INTO budget_reservations(reservation_id,month,day,cost_usd,created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(reservation_id) DO NOTHING",
            (reservation_id, month, day, max(0.0, float(cost_usd)), self.now()),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    @database_locked
    def settle_usage(self, reservation_id, month, day, cost_usd, input_tokens, output_tokens):
        with self.connection:
            exists = self.connection.execute(
                "SELECT 1 FROM budget_reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            if not exists:
                return False
            self.connection.execute(
                "INSERT INTO usage(month,day,cost_usd,input_tokens,output_tokens,created_at) VALUES(?,?,?,?,?,?)",
                (month, day, max(0.0, float(cost_usd)), int(input_tokens), int(output_tokens), self.now()),
            )
            self.connection.execute(
                "DELETE FROM budget_reservations WHERE reservation_id=?", (reservation_id,)
            )
            return True

    @database_locked
    def usage_totals(self, month, day):
        zone = ZoneInfo("America/Toronto")
        local_day = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=zone)
        day_start = local_day.astimezone(timezone.utc).isoformat(timespec="seconds")
        day_end = (local_day + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
        local_month = datetime.strptime(month, "%Y-%m").replace(tzinfo=zone)
        month_start = local_month.astimezone(timezone.utc).isoformat(timespec="seconds")
        if local_month.month == 12:
            next_month = local_month.replace(year=local_month.year + 1, month=1)
        else:
            next_month = local_month.replace(month=local_month.month + 1)
        month_end = next_month.astimezone(timezone.utc).isoformat(timespec="seconds")
        month_row = self.connection.execute(
            "SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) "
            "FROM usage WHERE created_at>=? AND created_at<?",
            (month_start, month_end),
        ).fetchone()
        day_row = self.connection.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM usage WHERE created_at>=? AND created_at<?",
            (day_start, day_end),
        ).fetchone()
        reserved_month = self.connection.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM budget_reservations WHERE month=?", (month,)
        ).fetchone()[0]
        reserved_day = self.connection.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM budget_reservations WHERE day=?", (day,)
        ).fetchone()[0]
        return {
            "monthly_cost": float(month_row[0]) + float(reserved_month),
            "monthly_input_tokens": int(month_row[1]),
            "monthly_output_tokens": int(month_row[2]),
            "daily_cost": float(day_row[0]) + float(reserved_day),
        }

    @database_locked
    def add_activity(self, event):
        cursor = self.connection.execute("INSERT INTO activity(event,created_at) VALUES(?,?)", (event, self.now()))
        self.connection.commit()
        return cursor.lastrowid

    @database_locked
    def create_diary_entry(self, local_date, content, record_type="diary", session_id="", event_ids=()):
        from .diary import validate_diary_content
        if not isinstance(local_date, str):
            raise ValueError("invalid diary date")
        try:
            if datetime.strptime(local_date, "%Y-%m-%d").date().isoformat() != local_date:
                raise ValueError
        except ValueError:
            raise ValueError("invalid diary date") from None
        content = validate_diary_content(content)
        if record_type not in {"diary", "incident_summary"}:
            raise ValueError("invalid diary record type")
        if not isinstance(event_ids, (list, tuple)) or len(event_ids) > 20:
            raise ValueError("invalid activity event references")
        event_ids = tuple(event_ids)
        if (len(set(event_ids)) != len(event_ids)
                or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in event_ids)):
            raise ValueError("invalid activity event references")
        entry_id = uuid.uuid4().hex
        with self.connection:
            if event_ids:
                marks = ",".join("?" for _ in event_ids)
                rows = self.connection.execute(
                    f"SELECT id FROM activity WHERE id IN ({marks})", event_ids
                ).fetchall()
                if {int(row[0]) for row in rows} != set(event_ids):
                    raise ValueError("unknown activity event reference")
            if record_type == "incident_summary" and session_id:
                existing = self.connection.execute(
                    "SELECT entry_id FROM diary_entries WHERE record_type='incident_summary' AND session_id=?",
                    (session_id,),
                ).fetchone()
                if existing:
                    return existing[0]
            if record_type == "diary" and self.connection.execute(
                "SELECT 1 FROM diary_entries WHERE record_type='diary' AND local_date=?", (local_date,)
            ).fetchone():
                raise ValueError("a diary entry already exists for this date")
            self.connection.execute(
                "INSERT INTO diary_entries(entry_id,local_date,created_at,session_id,content,record_type,delivery_state) "
                "VALUES(?,?,?,?,?,?, 'pending')",
                (entry_id, local_date, self.now(), session_id, content, record_type),
            )
            for event_id in event_ids:
                self.connection.execute(
                    "INSERT INTO diary_event_refs(entry_id,activity_id) VALUES(?,?)", (entry_id, event_id)
                )
            self.connection.execute(
                "INSERT INTO activity(event,created_at) VALUES(?,?)",
                (f"Diary record created ({entry_id})", self.now()),
            )
        return entry_id

    def _diary_row(self, row):
        if row is None:
            return None
        result = dict(row)
        result["event_ids"] = [item[0] for item in self.connection.execute(
            "SELECT activity_id FROM diary_event_refs WHERE entry_id=? ORDER BY activity_id", (result["entry_id"],)
        ).fetchall()]
        return result

    @database_locked
    def diary_entry(self, local_date=None):
        if local_date is None:
            row = self.connection.execute(
                "SELECT * FROM diary_entries WHERE record_type='diary' ORDER BY local_date DESC LIMIT 1"
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM diary_entries WHERE record_type='diary' AND local_date=?", (local_date,)
            ).fetchone()
        return self._diary_row(row)

    @database_locked
    def diary_entries(self, limit=7):
        limit = max(1, min(100, int(limit)))
        rows = self.connection.execute(
            "SELECT * FROM diary_entries WHERE record_type='diary' ORDER BY local_date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._diary_row(row) for row in rows]

    @database_locked
    def diary_incident_exists(self, session_id):
        return self.connection.execute(
            "SELECT 1 FROM diary_entries WHERE record_type='incident_summary' AND session_id=?", (session_id,)
        ).fetchone() is not None

    @database_locked
    def uncertain_diary_delivery(self, local_date):
        row = self.connection.execute(
            "SELECT * FROM diary_entries WHERE local_date=? AND delivery_state='uncertain' "
            "ORDER BY CASE record_type WHEN 'diary' THEN 0 ELSE 1 END,created_at DESC LIMIT 1",
            (local_date,),
        ).fetchone()
        return self._diary_row(row)

    @database_locked
    def pending_diary_deliveries(self, limit=20):
        rows = self.connection.execute(
            "SELECT * FROM diary_entries WHERE delivery_state='pending' ORDER BY created_at,entry_id LIMIT ?",
            (max(1, min(100, int(limit))),),
        ).fetchall()
        return [self._diary_row(row) for row in rows]

    @database_locked
    def diary_delivery_state(self, entry_id):
        row = self.connection.execute(
            "SELECT delivery_state FROM diary_entries WHERE entry_id=?", (entry_id,)
        ).fetchone()
        return row[0] if row else None

    @database_locked
    def claim_pending_diary_delivery(self, entry_id):
        cursor = self.connection.execute(
            "UPDATE diary_entries SET delivery_state='uncertain',delivery_attempts=delivery_attempts+1 "
            "WHERE entry_id=? AND delivery_state='pending'",
            (entry_id,),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    @database_locked
    def claim_diary_sender(self, entry_id):
        token = uuid.uuid4().hex
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE diary_entries SET delivery_state='uncertain',delivery_attempts=delivery_attempts+1 "
                "WHERE entry_id=? AND delivery_state='pending' "
                "AND NOT EXISTS(SELECT 1 FROM diary_delivery_claims WHERE entry_id=?)",
                (entry_id, entry_id))
            if cursor.rowcount != 1:
                return None
            self.connection.execute('INSERT INTO diary_delivery_claims VALUES(?,?)', (entry_id, token))
        return token

    @database_locked
    def finish_diary_sender(self, entry_id, token, delivered=False):
        with self.connection:
            cursor = self.connection.execute('DELETE FROM diary_delivery_claims WHERE entry_id=? AND token=?',
                                             (entry_id, token))
            if cursor.rowcount != 1:
                return False
            if delivered:
                return self.mark_diary_delivered(entry_id)
        return False

    @database_locked
    def recover_diary_claims(self):
        # Startup only: interrupted sends remain uncertain; no retry authorization.
        with self.connection:
            self.connection.execute('DELETE FROM diary_delivery_claims')

    @database_locked
    def mark_diary_delivered(self, entry_id):
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE diary_entries SET delivery_state='delivered' WHERE entry_id=? AND delivery_state='uncertain'",
                (entry_id,),
            )
            if cursor.rowcount == 1:
                self.connection.execute("DELETE FROM telegram_delivery_progress WHERE kind='diary' AND record_id=?", (entry_id,))
            return cursor.rowcount == 1

    @database_locked
    def retry_uncertain_diary_delivery(self, entry_id):
        # This method is invoked only by the explicit owner retry command.
        from .diary import format_diary_entry
        with self.connection:
            row = self.connection.execute("SELECT * FROM diary_entries WHERE entry_id=? AND delivery_state='uncertain'", (entry_id,)).fetchone()
            if row is None or self.connection.execute(
                    'SELECT 1 FROM diary_delivery_claims WHERE entry_id=?', (entry_id,)).fetchone():
                return False
            content = format_diary_entry(self._diary_row(row))
            self.connection.execute(
                "INSERT OR IGNORE INTO telegram_delivery_progress(kind,record_id,content) VALUES('diary',?,?)",
                (entry_id, content))
            self.connection.execute("UPDATE telegram_delivery_progress SET chunk_attempts=0,retry_notice=1 WHERE kind='diary' AND record_id=?", (entry_id,))
            self.connection.execute("UPDATE diary_entries SET delivery_state='pending' WHERE entry_id=?", (entry_id,))
            return True

    @database_locked
    def health_snapshot(self, owner_id, now=None):
        current = datetime.now(timezone.utc) if now is None else now
        messages = {row['queued']: row['n'] for row in self.connection.execute(
            "SELECT queued,count(*) AS n FROM messages WHERE owner_id=? AND role='user' GROUP BY queued",
            (owner_id,))}
        inbox = self.connection.execute(
            "SELECT count(*) AS n,min(received_at) AS oldest FROM telegram_inbox "
            "WHERE json_extract(payload,'$.message.from.id')=? AND json_extract(payload,'$.message.chat.id')=?",
            (owner_id, owner_id)).fetchone()
        held_inbox = self.connection.execute(
            "SELECT count(*) FROM telegram_uncertain WHERE json_extract(payload,'$.message.from.id')=? "
            "AND json_extract(payload,'$.message.chat.id')=?", (owner_id, owner_id)).fetchone()[0]
        outbox = {lane: {'pending': 0, 'sending': 0, 'held': 0, 'oldest_seconds': None}
                  for lane in ('normal', 'system')}
        for row in self.connection.execute(
                "SELECT lane,delivery_state,count(*) AS n,min(created_at) AS oldest FROM telegram_outbox "
                "WHERE chat_id=? GROUP BY lane,delivery_state", (owner_id,)):
            if row['lane'] in outbox and row['delivery_state'] in ('pending', 'sending', 'held'):
                lane = outbox[row['lane']]
                lane[row['delivery_state']] = row['n']
                if row['delivery_state'] == 'pending':
                    lane['oldest_seconds'] = wall_age_seconds(row['oldest'], current)
        diary = {row[0]: row[1] for row in self.connection.execute(
            'SELECT delivery_state,count(*) FROM diary_entries GROUP BY delivery_state')}
        return {'queued_messages': messages.get(1, 0), 'active_messages': messages.get(2, 0),
                'held_messages': messages.get(3, 0), 'inbox': inbox['n'], 'held_inbox': held_inbox,
                'inbox_oldest_seconds': wall_age_seconds(inbox['oldest'], current),
                'outbox': outbox, 'diary_pending': diary.get('pending', 0),
                'diary_uncertain': diary.get('uncertain', 0),
                'paused': self.get_state('paused', '0') == '1',
                'sleeping': self.get_state('sleeping', '0') == '1',
                'model_halted': bool(self.get_state('autonomy_error', ''))}

    @database_locked
    def recent_activity(self, limit=10):
        return self.connection.execute(
            "SELECT id,event,created_at FROM activity ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    @database_locked
    def create_memory_record(self, record_type, content, source, event_ids=(), diary_ids=()):
        if record_type not in {"experience", "interest"}:
            raise ValueError("invalid memory record type")
        if not isinstance(content, str) or not content.strip() or len(content) > 4000:
            raise ValueError("invalid memory content")
        if source not in {"andrew", "owner"}:
            raise ValueError("invalid memory source")
        event_ids = self._validate_reference_ids(event_ids, int)
        diary_ids = self._validate_reference_ids(diary_ids, str)
        record_id = uuid.uuid4().hex
        created_at = self.now()
        with self.connection:
            self._validate_memory_references(event_ids, diary_ids)
            self.connection.execute(
                "INSERT INTO memory_records(record_id,record_type,created_at,content,source) VALUES(?,?,?,?,?)",
                (record_id, record_type, created_at, content.strip(), source),
            )
            self._insert_memory_references(
                "memory_event_refs", "memory_diary_refs", "record_id", record_id, event_ids, diary_ids
            )
            self.connection.execute(
                "INSERT INTO memory_fts(record_id,record_type,created_at,content,source) VALUES(?,?,?,?,?)",
                (record_id, record_type, created_at, content.strip(), source),
            )
            self.connection.execute(
                "INSERT INTO activity(event,created_at) VALUES(?,?)",
                (f"Memory record created ({record_id}; type={record_type})", created_at),
            )
        return record_id

    @database_locked
    def ensure_founding_letter(self, default_content):
        """Prefer the owner's archived letter; never replace it with a code default."""
        row = self.connection.execute(
            "SELECT * FROM memory_records WHERE source='owner' AND record_id LIKE 'owner-letter-%' "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1"
        ).fetchone()
        content = row['content'] if row else default_content
        if not isinstance(content, str) or not content.strip() or len(content) > 4000:
            raise ValueError('invalid founding letter')
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        record_id = 'owner-letter-' + digest
        if row and row['record_id'] != record_id:
            raise ValueError('founding letter archive hash mismatch')
        with self.connection:
            if row is None:
                now = self.now()
                self.connection.execute(
                    'INSERT INTO memory_records(record_id,record_type,created_at,content,source) VALUES(?,?,?,?,?)',
                    (record_id, 'experience', now, content, 'owner'))
                self.connection.execute(
                    'INSERT INTO memory_fts(record_id,record_type,created_at,content,source) VALUES(?,?,?,?,?)',
                    (record_id, 'experience', now, content, 'owner'))
            for operation in ('UPDATE', 'DELETE'):
                self.connection.execute(
                    f'CREATE TRIGGER IF NOT EXISTS protect_owner_letter_{digest}_{operation.lower()} '
                    f"BEFORE {operation} ON memory_records WHEN OLD.record_id='{record_id}' "
                    "BEGIN SELECT RAISE(ABORT,'Owner letter is immutable'); END")
            if self.get_state('founding_letter_record_id', '') != record_id:
                # Old versions marked agent creation as delivery. That is not a receipt.
                for key, value in (('founding_letter_delivered', '0'), ('founding_letter_record_id', record_id)):
                    self.connection.execute('INSERT INTO state(key,value) VALUES(?,?) '
                                            'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
        return {'record_id': record_id, 'content': content, 'digest': digest}

    @database_locked
    def queue_founding_letter(self, owner_id, letter):
        marker = 'owner_letter_queued:' + letter['digest']
        with self.connection:
            existing = self.connection.execute('SELECT value FROM state WHERE key=?', (marker,)).fetchone()
            if existing:
                return int(existing[0])
            cursor = self.connection.execute(
                'INSERT INTO messages(owner_id,role,content,created_at,queued) VALUES(?,?,?,?,1)',
                (owner_id, 'user', letter['content'], self.now()))
            self.connection.execute('INSERT INTO state(key,value) VALUES(?,?)', (marker, str(cursor.lastrowid)))
            return cursor.lastrowid

    @database_locked
    def confirm_founding_letter_delivery(self, letter):
        with self.connection:
            if self.get_state('founding_letter_record_id', '') != letter['record_id']:
                raise ValueError('founding letter changed during delivery')
            self.connection.execute(
                "INSERT INTO state(key,value) VALUES('founding_letter_delivered','1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'")
            row = self.connection.execute('SELECT value FROM state WHERE key=?',
                                          ('owner_letter_queued:' + letter['digest'],)).fetchone()
            if row:
                self.connection.execute('UPDATE messages SET queued=0 WHERE id=? AND content=? AND queued IN (1,2,3)',
                                        (int(row[0]), letter['content']))

    @database_locked
    def create_personality_revision(self, content, reason, source, event_ids=(), diary_ids=()):
        if not isinstance(content, str) or not content.strip() or len(content) > 4000:
            raise ValueError("invalid personality content")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("invalid personality revision reason")
        if source not in {"andrew", "owner"}:
            raise ValueError("invalid personality revision source")
        event_ids = self._validate_reference_ids(event_ids, int)
        diary_ids = self._validate_reference_ids(diary_ids, str)
        revision_id = uuid.uuid4().hex
        created_at = self.now()
        effective_date = datetime.now(ZoneInfo("America/Toronto")).date().isoformat()
        with self.connection:
            self._validate_memory_references(event_ids, diary_ids)
            self.connection.execute(
                "INSERT INTO personality_revisions(revision_id,created_at,effective_date,content,reason,source) "
                "VALUES(?,?,?,?,?,?)",
                (revision_id, created_at, effective_date, content.strip(), reason.strip(), source),
            )
            self._insert_memory_references(
                "personality_event_refs", "personality_diary_refs", "revision_id", revision_id,
                event_ids, diary_ids,
            )
            self.connection.execute(
                "INSERT INTO memory_fts(record_id,record_type,created_at,content,source) VALUES(?,?,?,?,?)",
                (revision_id, "personality", created_at, content.strip(), source),
            )
            self.connection.execute(
                "INSERT INTO activity(event,created_at) VALUES(?,?)",
                (f"Personality revision created ({revision_id})", created_at),
            )
        return revision_id

    @staticmethod
    def _validate_reference_ids(values, expected_type):
        if not isinstance(values, (list, tuple)) or len(values) > 20:
            raise ValueError("invalid memory references")
        result = tuple(values)
        if len(set(result)) != len(result) or any(
            isinstance(item, bool) or not isinstance(item, expected_type) or
            (expected_type is int and item <= 0) or (expected_type is str and not item)
            for item in result
        ):
            raise ValueError("invalid memory references")
        return result

    def _validate_memory_references(self, event_ids, diary_ids):
        if event_ids:
            marks = ",".join("?" for _ in event_ids)
            rows = self.connection.execute(f"SELECT id FROM activity WHERE id IN ({marks})", event_ids).fetchall()
            if {int(row[0]) for row in rows} != set(event_ids):
                raise ValueError("unknown activity event reference")
        if diary_ids:
            marks = ",".join("?" for _ in diary_ids)
            rows = self.connection.execute(
                f"SELECT entry_id FROM diary_entries WHERE entry_id IN ({marks})", diary_ids
            ).fetchall()
            if {row[0] for row in rows} != set(diary_ids):
                raise ValueError("unknown diary reference")

    def _insert_memory_references(self, event_table, diary_table, id_column, record_id, event_ids, diary_ids):
        self.connection.executemany(
            f"INSERT INTO {event_table}({id_column},activity_id) VALUES(?,?)",
            [(record_id, event_id) for event_id in event_ids],
        )
        self.connection.executemany(
            f"INSERT INTO {diary_table}({id_column},entry_id) VALUES(?,?)",
            [(record_id, diary_id) for diary_id in diary_ids],
        )

    def _memory_references(self, event_table, diary_table, id_column, record_id):
        events = self.connection.execute(
            f"SELECT activity_id FROM {event_table} WHERE {id_column}=? ORDER BY activity_id", (record_id,)
        ).fetchall()
        diaries = self.connection.execute(
            f"SELECT entry_id FROM {diary_table} WHERE {id_column}=? ORDER BY entry_id", (record_id,)
        ).fetchall()
        return [row[0] for row in events], [row[0] for row in diaries]

    @database_locked
    def current_personality_revision(self):
        row = self.connection.execute(
            "SELECT * FROM personality_revisions ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return self._personality_revision_row(row)

    @database_locked
    def personality_revisions(self, limit=20):
        limit = max(1, min(100, int(limit)))
        rows = self.connection.execute(
            "SELECT * FROM personality_revisions ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._personality_revision_row(row) for row in rows]

    def _personality_revision_row(self, row):
        if row is None:
            return None
        result = dict(row)
        result["event_ids"], result["diary_ids"] = self._memory_references(
            "personality_event_refs", "personality_diary_refs", "revision_id", result["revision_id"]
        )
        return result

    @database_locked
    def get_memory_record(self, record_id):
        row = self.connection.execute(
            "SELECT * FROM memory_records WHERE record_id=?", (record_id,)
        ).fetchone()
        if row is not None:
            result = dict(row)
            result["event_ids"], result["diary_ids"] = self._memory_references(
                "memory_event_refs", "memory_diary_refs", "record_id", record_id
            )
            return result
        row = self.connection.execute(
            "SELECT * FROM personality_revisions WHERE revision_id=?", (record_id,)
        ).fetchone()
        return self._personality_revision_row(row)

    @database_locked
    def search_memory_rows(self, match_query, limit=8):
        limit = max(1, min(20, int(limit)))
        rows = self.connection.execute(
            "SELECT record_id,record_type,created_at,content,source,bm25(memory_fts) AS rank "
            "FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank ASC,created_at DESC LIMIT ?",
            (match_query, limit),
        ).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            if result["record_type"] == "personality":
                events, diaries = self._memory_references(
                    "personality_event_refs", "personality_diary_refs", "revision_id", result["record_id"]
                )
            else:
                events, diaries = self._memory_references(
                    "memory_event_refs", "memory_diary_refs", "record_id", result["record_id"]
                )
            result["event_ids"] = events
            result["diary_ids"] = diaries
            results.append(result)
        return results
