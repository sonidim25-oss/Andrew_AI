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
from .telegram_menu import owner_control


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
        self.connection.create_function('andrew_owner_control', 3,
            lambda payload, owner_id, after: int(owner_control(json.loads(payload), owner_id, after)),
            deterministic=True)
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
            CREATE TABLE IF NOT EXISTS session_history (
                id INTEGER PRIMARY KEY,
                source_key TEXT NOT NULL UNIQUE,
                owner_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
                source TEXT NOT NULL CHECK(source IN ('controller','letta')),
                session_id TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                content TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                redacted INTEGER NOT NULL,
                complete INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS session_history_owner ON session_history(owner_id,session_id,id);
            CREATE VIRTUAL TABLE IF NOT EXISTS session_history_fts USING fts5(
                content, content='session_history', content_rowid='id', tokenize='unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS session_history_insert AFTER INSERT ON session_history BEGIN
                INSERT INTO session_history_fts(rowid,content) VALUES(new.id,new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS session_history_update AFTER UPDATE OF content ON session_history BEGIN
                INSERT INTO session_history_fts(session_history_fts,rowid,content) VALUES('delete',old.id,old.content);
                INSERT INTO session_history_fts(rowid,content) VALUES(new.id,new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS session_history_delete AFTER DELETE ON session_history BEGIN
                INSERT INTO session_history_fts(session_history_fts,rowid,content) VALUES('delete',old.id,old.content);
            END;
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
            CREATE TABLE IF NOT EXISTS telegram_chunk_receipts (
                attempt_id TEXT PRIMARY KEY,
                delivery_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                record_id TEXT NOT NULL,
                chunk INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                wire_sha256 TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                UNIQUE(delivery_id,chunk)
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
            CREATE TABLE IF NOT EXISTS memory_provenance (
                record_id TEXT PRIMARY KEY,
                origin TEXT NOT NULL CHECK(origin IN ('owner','agent','untrusted','system')),
                basis TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_history_refs (
                record_id TEXT NOT NULL REFERENCES memory_provenance(record_id),
                history_id INTEGER NOT NULL REFERENCES session_history(id),
                PRIMARY KEY(record_id,history_id)
            );
            CREATE TRIGGER IF NOT EXISTS memory_provenance_immutable_update BEFORE UPDATE ON memory_provenance BEGIN
                SELECT RAISE(ABORT,'Memory provenance is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS memory_provenance_immutable_delete BEFORE DELETE ON memory_provenance BEGIN
                SELECT RAISE(ABORT,'Memory provenance is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS memory_history_refs_immutable_update BEFORE UPDATE ON memory_history_refs BEGIN
                SELECT RAISE(ABORT,'Memory source links are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS memory_history_refs_immutable_delete BEFORE DELETE ON memory_history_refs BEGIN
                SELECT RAISE(ABORT,'Memory source links are immutable');
            END;
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
            CREATE TABLE IF NOT EXISTS cycle_recovery_checkpoints (
                cycle_id TEXT PRIMARY KEY REFERENCES cycles(cycle_id),
                phase TEXT NOT NULL CHECK(phase IN ('prepared','model_started','model_confirmed')),
                owner_revision TEXT NOT NULL,
                research_id TEXT NOT NULL,
                research_revision INTEGER NOT NULL,
                activity_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_turn_receipts (
                cycle_id TEXT PRIMARY KEY REFERENCES cycles(cycle_id),
                session_id TEXT NOT NULL,
                owner_revision TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                request_id TEXT NOT NULL UNIQUE,
                client_message_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('prepared','accepted','completed')),
                run_id TEXT NOT NULL DEFAULT '',
                terminal_id TEXT NOT NULL DEFAULT '',
                assistant_ids TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cycle_tool_attempts (
                attempt_id TEXT PRIMARY KEY,
                cycle_id TEXT NOT NULL REFERENCES cycles(cycle_id),
                tool_name TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('started','returned','uncertain','not_executed')),
                started_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS cycle_tool_attempts_cycle ON cycle_tool_attempts(cycle_id,outcome);
            CREATE TABLE IF NOT EXISTS provider_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                reservation_id TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('dispatching','not_reached','completed','uncertain')),
                created_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS provider_attempts_cycle ON provider_attempts(cycle_id,outcome);
            CREATE TABLE IF NOT EXISTS external_action_receipts (
                action_id TEXT PRIMARY KEY,
                adapter TEXT NOT NULL,
                operation TEXT NOT NULL,
                request_id TEXT NOT NULL,
                digest TEXT NOT NULL,
                principal TEXT NOT NULL,
                lookup TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('uncertain','confirmed')),
                receipt TEXT,
                attempt_id TEXT REFERENCES cycle_tool_attempts(attempt_id),
                session_id TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                checked_at REAL NOT NULL DEFAULT 0,
                UNIQUE(adapter,request_id)
            );
            CREATE INDEX IF NOT EXISTS external_action_pending ON external_action_receipts(adapter,state,checked_at);
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
        for name in ('delivery_id', 'chunk_token'):
            if name not in progress_columns:
                self.connection.execute('ALTER TABLE telegram_delivery_progress ADD COLUMN ' + name + " TEXT NOT NULL DEFAULT ''")
        for row in self.connection.execute("SELECT kind,record_id FROM telegram_delivery_progress WHERE delivery_id=''").fetchall():
            self.connection.execute('UPDATE telegram_delivery_progress SET delivery_id=? WHERE kind=? AND record_id=? AND delivery_id=\'\'', (uuid.uuid4().hex, row['kind'], row['record_id']))
        self.connection.commit()
        with self.connection:
            for table, identifier in (('memory_records', 'record_id'), ('personality_revisions', 'revision_id')):
                self.connection.execute(f'''INSERT OR IGNORE INTO memory_provenance
                    SELECT {identifier},CASE source WHEN 'owner' THEN 'owner' WHEN 'system' THEN 'system'
                    ELSE 'untrusted' END,'legacy_unverified',created_at FROM {table}''')
        self.backfill_session_history()
        from .recovery_records import initialize_schema
        initialize_schema(self)
        from .runner_recovery import initialize_schema as initialize_runner_schema
        try:
            initialize_runner_schema(self)
        except BaseException:
            self.connection.close()
            raise
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
        with self.connection:
            created_at = self.now()
            cursor = self.connection.execute(
                "INSERT INTO messages(owner_id, role, content, created_at, queued) VALUES(?,?,?,?,?)",
                (owner_id, role, content, created_at, int(queued)),
            )
            message_id = cursor.lastrowid
            if role in {'user', 'assistant'}:
                self._archive_history('controller:' + str(message_id), owner_id, role, content,
                    source='controller', source_message_id=str(message_id), created_at=created_at)
            return message_id

    def _archive_history(self, source_key, owner_id, role, content, *, source='letta', session_id=None,
                         cycle_id=None, conversation_id='', source_message_id='', tool_name='',
                         complete=True, created_at=None):
        from .memory import redact_history_text
        content, redacted = redact_history_text(content)
        now = self.now()
        self.connection.execute('''INSERT INTO session_history
            (source_key,owner_id,role,source,session_id,cycle_id,conversation_id,source_message_id,
             tool_name,content,content_sha256,redacted,complete,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_key) DO UPDATE SET
              content=excluded.content,content_sha256=excluded.content_sha256,redacted=excluded.redacted,
              complete=excluded.complete,updated_at=excluded.updated_at
            WHERE session_history.owner_id=excluded.owner_id AND session_history.complete=0''',
            (source_key, owner_id, role, source,
             self.get_state('active_session_id', '') if session_id is None else session_id,
             self.get_state('active_cycle_id', '') if cycle_id is None else cycle_id,
             conversation_id, source_message_id, tool_name, content,
             hashlib.sha256(content.encode('utf-8')).hexdigest(), int(redacted), int(complete), created_at or now, now))

    @database_locked
    def archive_history(self, source_key, owner_id, role, content, **metadata):
        if type(owner_id) is not int or role not in {'user', 'assistant', 'tool'} or not isinstance(content, str):
            raise ValueError('invalid history record')
        with self.connection:
            self._archive_history(source_key, owner_id, role, content, **metadata)

    @database_locked
    def backfill_session_history(self):
        """Index retained controller chat once; do not invent historical session IDs."""
        after = int(self.get_state('session_history_backfill_message_id', '0'))
        while True:
            rows = self.connection.execute('SELECT * FROM messages WHERE id>? ORDER BY id LIMIT 500', (after,)).fetchall()
            if not rows:
                return
            with self.connection:
                for row in rows:
                    if row['role'] in {'user', 'assistant'}:
                        self._archive_history('controller:' + str(row['id']), row['owner_id'], row['role'], row['content'],
                            source='controller', session_id='', cycle_id='', source_message_id=str(row['id']), created_at=row['created_at'])
                after = rows[-1]['id']
                self.connection.execute("INSERT INTO state(key,value) VALUES('session_history_backfill_message_id',?) "
                                        'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (str(after),))

    @database_locked
    def search_session_history(self, owner_id, match_query, limit, session_id=None):
        return [dict(row) for row in self.connection.execute('''
            SELECT h.id,h.role,h.source,h.session_id,h.cycle_id,h.conversation_id,h.source_message_id,
                   h.tool_name,h.content_sha256,h.redacted,h.complete,h.created_at,length(replace(h.content,char(0),'?')) AS characters,
                   snippet(session_history_fts,0,'[',']',' … ',36) AS excerpt
            FROM session_history_fts JOIN session_history h ON h.id=session_history_fts.rowid
            WHERE session_history_fts MATCH ? AND h.owner_id=? AND (? IS NULL OR h.session_id=?)
            ORDER BY bm25(session_history_fts),h.id DESC LIMIT ?''',
            (match_query, owner_id, session_id, session_id, limit)).fetchall()]

    @database_locked
    def read_session_history(self, owner_id, record_id, offset, limit):
        row = self.connection.execute('''SELECT id,role,source,session_id,cycle_id,conversation_id,
            source_message_id,tool_name,content_sha256,redacted,complete,created_at,content
            FROM session_history WHERE id=? AND owner_id=?''', (record_id, owner_id)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['characters'] = len(result['content'])
        result['content'] = result['content'][offset:offset + limit]
        return result

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
        rows=self.connection.execute(
            "SELECT update_id,json_extract(payload,'$.message.text') AS text FROM telegram_inbox WHERE json_extract(payload,'$.message.from.id')=? "
            "AND json_extract(payload,'$.message.chat.id')=? "
            "AND json_extract(payload,'$.message.chat.type')='private' "
            "AND json_type(payload,'$.message.text')='text' "
            "AND trim(json_extract(payload,'$.message.text'))<>'' "
            "AND json_type(payload,'$.message.forward_origin') IS NULL "
            "AND json_type(payload,'$.message.forward_from') IS NULL "
            "AND json_type(payload,'$.message.forward_from_chat') IS NULL "
            "AND json_type(payload,'$.message.forward_sender_name') IS NULL "
            "AND update_id NOT IN (SELECT update_id FROM telegram_claims) ",
            (owner_id, owner_id),
        ).fetchall()
        from .telegram_menu import is_model_input
        prompt_after=int(self.get_state("telegram_wake_prompt_after_update_id","-1")) if self.get_state("telegram_wake_prompt_until","") else None
        return any(is_model_input(row["text"],update_id=row["update_id"],prompt_after=prompt_after) for row in rows) or self.connection.execute(
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
                               'active_session_status': 'running', 'session_start_requested': '',
                               'session_recovery_checkpoint': ''},
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
            entry_id = self._focused_research_id()
            entry = self.research_entry(entry_id) if entry_id else None
            self.connection.execute(
                'INSERT INTO cycle_recovery_checkpoints VALUES(?,?,?,?,?,?,?)',
                (cycle_id, 'prepared', self.get_state('owner_control_revision', '0'),
                 entry_id, entry['revision'] if entry else 0,
                 self.connection.execute('SELECT coalesce(max(id),0) FROM activity').fetchone()[0], self.now()),
            )

    @database_locked
    def checkpoint_cycle_turn(self, cycle_id, phase):
        """Commit a boundary before model admission and after the whole turn returns."""
        previous = {'model_started': 'prepared', 'model_confirmed': 'model_started'}.get(phase)
        if previous is None:
            raise ValueError('invalid cycle checkpoint phase')
        with self.connection:
            entry_id = self._focused_research_id()
            entry = self.research_entry(entry_id) if entry_id else None
            cursor = self.connection.execute(
                'UPDATE cycle_recovery_checkpoints SET phase=?,updated_at=?,research_id=?,research_revision=?,activity_id=? '
                'WHERE cycle_id=? AND phase=? '
                "AND EXISTS(SELECT 1 FROM cycles WHERE cycle_id=? AND status='running')",
                (phase, self.now(), entry_id, entry['revision'] if entry else 0,
                 self.connection.execute('SELECT coalesce(max(id),0) FROM activity').fetchone()[0],
                 cycle_id, previous, cycle_id),
            )
            if cursor.rowcount != 1:
                raise ValueError('cycle checkpoint is missing or out of order')

    @database_locked
    def record_model_turn_receipt(self, session_id, cycle_id, runtime, event):
        """Only the protocol bridge supplies these events; never store input text."""
        with self.connection:
            if event['kind'] == 'prepared':
                checkpoint = self.connection.execute(
                    'SELECT k.owner_revision FROM cycle_recovery_checkpoints k JOIN cycles c ON c.cycle_id=k.cycle_id '
                    "WHERE c.cycle_id=? AND c.session_id=? AND c.status='running' AND k.phase='model_started'",
                    (cycle_id, session_id)).fetchone()
                if (checkpoint is None or self.get_state('active_session_id', '') != session_id
                        or self.get_state('active_cycle_id', '') != cycle_id
                        or checkpoint['owner_revision'] != self.get_state('owner_control_revision', '0')):
                    raise ValueError('Model input has no current cycle admission')
                self.connection.execute(
                    "INSERT INTO model_turn_receipts(cycle_id,session_id,owner_revision,agent_id,conversation_id,request_id,client_message_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'prepared',?,?)",
                    (cycle_id, session_id, checkpoint['owner_revision'], runtime['agent_id'], runtime['conversation_id'],
                     event['request_id'], event['client_message_id'], self.now(), self.now()))
                return True
            row = self.connection.execute('SELECT * FROM model_turn_receipts WHERE cycle_id=? AND session_id=? AND request_id=?',
                (cycle_id, session_id, event['request_id'])).fetchone()
            if (row is None or row['agent_id'] != runtime['agent_id'] or row['conversation_id'] != runtime['conversation_id']):
                raise ValueError('Model receipt identity does not match admission')
            if event['kind'] == 'accepted' and row['state'] == 'prepared':
                self.connection.execute("UPDATE model_turn_receipts SET state='accepted',updated_at=? WHERE cycle_id=?", (self.now(), cycle_id))
                return True
            if event['kind'] == 'completed' and row['state'] == 'accepted':
                terminal = event['terminal']
                valid_id = lambda value: isinstance(value, str) and 0 < len(value) <= 256 and all(c.isascii() and (c.isalnum() or c in '._~:%-') for c in value)
                consumers = terminal.get('terminal_consumer_ids')
                if (terminal.get('type') != 'turn_finished' or terminal.get('stop_reason') != 'end_turn'
                        or terminal.get('error') or not valid_id(terminal.get('run_id')) or not valid_id(terminal.get('turn_id'))
                        or not isinstance(consumers, list) or row['request_id'] not in consumers):
                    return False
                ids = event.get('assistant_ids', [])
                if not isinstance(ids, list) or not ids or len(ids) > 256 or not all(valid_id(value) for value in ids):
                    return False
                self.connection.execute("UPDATE model_turn_receipts SET state='completed',run_id=?,terminal_id=?,assistant_ids=?,updated_at=? WHERE cycle_id=?",
                    (terminal['run_id'], terminal['turn_id'], json.dumps(ids), self.now(), cycle_id))
                return True
            return False

    @database_locked
    def promote_completed_model_turn(self, session_id):
        """Bridge the crash gap after a bound terminal receipt, without inference."""
        with self.connection:
            cycle_id = self.get_state('active_cycle_id', '')
            row = self.connection.execute(
                'SELECT r.*,k.phase,k.owner_revision AS checkpoint_revision FROM model_turn_receipts r '
                'JOIN cycle_recovery_checkpoints k ON k.cycle_id=r.cycle_id JOIN cycles c ON c.cycle_id=r.cycle_id '
                "WHERE r.cycle_id=? AND r.session_id=? AND r.state='completed' AND c.session_id=? AND c.status='running'",
                (cycle_id, session_id, session_id)).fetchone()
            if (row is None or row['phase'] != 'model_started'
                    or not session_id or self.get_state('active_session_id', '') != session_id
                    or self.get_state('active_session_status', '') != 'running'
                    or self.get_state('autonomy_error', '')
                    or row['owner_revision'] != self.get_state('owner_control_revision', '0')
                    or row['checkpoint_revision'] != row['owner_revision']
                    or row['agent_id'] != self.get_state('letta_agent_id', '')
                    or row['conversation_id'] != self.get_state('letta_conversation_id', '')
                    or any(self.get_state(key, '0') == '1' for key in ('paused','sleeping','restore_recovery_required'))):
                return False
            self.checkpoint_cycle_turn(cycle_id, 'model_confirmed')
            self.connection.execute('INSERT INTO activity(event,created_at) VALUES(?,?)',
                ('Model turn completion recovered from bound protocol receipt; no input or tool replayed', self.now()))
            return True

    @database_locked
    def begin_cycle_tool(self, context, name):
        """Persist admission before invoking a tool, without its arguments."""
        session_id, cycle_id, _ = context
        if not cycle_id:
            return None
        with self.connection:
            if not self.connection.execute(
                "SELECT 1 FROM cycles WHERE cycle_id=? AND session_id=? AND status='running'",
                (cycle_id, session_id),
            ).fetchone():
                raise ValueError('tool cycle is no longer running')
            attempt_id = uuid.uuid4().hex
            self.connection.execute('INSERT INTO cycle_tool_attempts VALUES(?,?,?,?,?,NULL)',
                                    (attempt_id, cycle_id, name, 'started', self.now()))
            return attempt_id

    @database_locked
    def finish_cycle_tool(self, attempt_id, confirmed):
        if attempt_id is None:
            return
        with self.connection:
            changed = self.connection.execute(
                "UPDATE cycle_tool_attempts SET outcome=?,finished_at=? WHERE attempt_id=? AND outcome='started'",
                ('returned' if confirmed else 'uncertain', self.now(), attempt_id),
            ).rowcount
            if changed != 1:
                if self.connection.execute(
                    "SELECT 1 FROM cycle_tool_attempts t JOIN external_action_receipts r ON r.attempt_id=t.attempt_id "
                    "WHERE t.attempt_id=? AND t.outcome='returned' AND r.state='confirmed'", (attempt_id,)).fetchone():
                    return
                raise ValueError('tool attempt is missing or already settled')

    @database_locked
    def external_action(self, adapter, request_id):
        row = self.connection.execute('SELECT * FROM external_action_receipts WHERE adapter=? AND request_id=?',
                                      (adapter, request_id)).fetchone()
        return dict(row) if row else None

    @database_locked
    def pending_external_actions(self, adapter, limit=1):
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM external_action_receipts WHERE adapter=? AND state='uncertain' ORDER BY checked_at,created_at LIMIT ?",
            (adapter, limit))]

    @database_locked
    def claim_external_action(self, adapter, operation, request_id, digest, principal, lookup, context):
        """Commit before dispatch; unknown earlier Gmail mutations block new IDs too."""
        if adapter != 'gmail' or operation not in {'send', 'draft', 'delete'}:
            raise ValueError('Unsupported action adapter')
        with self.connection:
            if self.external_action(adapter, request_id):
                return None
            if self.pending_external_actions(adapter):
                raise ValueError('An earlier action is uncertain; reconcile it before another mutation')
            if adapter == 'gmail':
                for row in self.connection.execute("SELECT value FROM state WHERE key LIKE 'gmail_mutation:%'"):
                    try:
                        confirmed = json.loads(row[0]).get('state') == 'confirmed'
                    except (ValueError, AttributeError):
                        confirmed = False
                    if not confirmed:
                        raise ValueError('An earlier Gmail outcome is uncertain; legacy records require manual inspection')
            attempt_id, session_id, cycle_id = context or (None, '', '')
            if attempt_id and not self.connection.execute(
                    'SELECT 1 FROM cycle_tool_attempts t JOIN cycles c ON c.cycle_id=t.cycle_id '
                    "WHERE t.attempt_id=? AND t.tool_name=? AND t.outcome='started' AND c.session_id=? AND c.cycle_id=?",
                    (attempt_id, 'gmail.' + operation, session_id, cycle_id)).fetchone():
                raise ValueError('Action context does not match tool admission')
            if cycle_id and not attempt_id:
                raise ValueError('Action cycle requires a tool admission')
            action_id = uuid.uuid4().hex
            self.connection.execute('''INSERT INTO external_action_receipts
                (action_id,adapter,operation,request_id,digest,principal,lookup,state,attempt_id,session_id,cycle_id,created_at)
                VALUES(?,?,?,?,?,?,?,'uncertain',?,?,?,?)''',
                (action_id, adapter, operation, request_id, digest, principal, json.dumps(lookup), attempt_id, session_id, cycle_id, self.now()))
            self.connection.execute('INSERT INTO state(key,value) VALUES(?,?)',
                ('gmail_mutation:' + request_id, json.dumps({'digest': digest, 'state': 'uncertain'})))
            return self.external_action(adapter, request_id)

    @database_locked
    def confirm_external_action(self, action_id, principal, result, evidence):
        with self.connection:
            row = self.connection.execute('SELECT * FROM external_action_receipts WHERE action_id=? AND principal=?',
                                          (action_id, principal)).fetchone()
            if row is None:
                raise ValueError('Action account binding does not match')
            if (row['adapter'] != 'gmail' or evidence not in {'provider_response', 'gmail_sent_message', 'gmail_draft', 'gmail_trash'}
                    or set(result) != {'id', 'operation', 'confirmed'} or result['operation'] != row['operation']
                    or result['confirmed'] is not True or not isinstance(result['id'], str) or not result['id']):
                raise ValueError('Invalid action receipt')
            if row['state'] == 'confirmed':
                return json.loads(row['receipt'])['result']
            receipt = {'result': result, 'evidence': evidence, 'confirmed_at': self.now()}
            self.connection.execute("UPDATE external_action_receipts SET state='confirmed',receipt=? WHERE action_id=?",
                                    (json.dumps(receipt), action_id))
            self.connection.execute('UPDATE state SET value=? WHERE key=?',
                (json.dumps({'digest': row['digest'], 'state': 'confirmed', 'result': result}), 'gmail_mutation:' + row['request_id']))
            if row['attempt_id']:
                self.connection.execute("UPDATE cycle_tool_attempts SET outcome='returned',finished_at=? "
                    'WHERE attempt_id=? AND tool_name=?', (self.now(), row['attempt_id'], 'gmail.' + row['operation']))
            self.connection.execute('INSERT INTO activity(event,created_at) VALUES(?,?)',
                (f"External action confirmed (adapter={row['adapter']}, operation={row['operation']}, id={action_id}, evidence={evidence})", self.now()))
            return result

    @database_locked
    def check_external_action_due(self, action_id, now, interval=60):
        with self.connection:
            return self.connection.execute("UPDATE external_action_receipts SET checked_at=? WHERE action_id=? "
                "AND state='uncertain' AND checked_at<=?", (now, action_id, now - interval)).rowcount == 1

    @database_locked
    def cycle_uncertain_effects(self, cycle_id, *, ignore_attempt_id=None):
        names = ('gmail.send', 'gmail.draft', 'gmail.delete', 'python.run', 'node.run', 'shell.run',
                 'browser.open', 'browser.control', 'browser.account', 'voice.generate', 'image.generate',
                 'image.edit', 'profile.propose_avatar')
        marks = ','.join('?' for _ in names)
        return bool(self.connection.execute(
            f"SELECT 1 FROM cycle_tool_attempts WHERE cycle_id=? AND outcome IN ('started','uncertain') AND tool_name IN ({marks}) AND (? IS NULL OR attempt_id!=?) LIMIT 1",
            (cycle_id, *names, ignore_attempt_id, ignore_attempt_id)).fetchone())

    @database_locked
    def hold_confirmed_actions(self, session_id, cycle_id):
        """Hold a completed model turn while its known external actions are checked."""
        with self.connection:
            checkpoint = self.connection.execute('SELECT * FROM cycle_recovery_checkpoints WHERE cycle_id=?', (cycle_id,)).fetchone()
            if (not checkpoint or checkpoint['phase'] != 'model_confirmed'
                    or checkpoint['owner_revision'] != self.get_state('owner_control_revision', '0')
                    or self.get_state('active_session_id', '') != session_id
                    or self.get_state('autonomy_error', '')
                    or any(self.get_state(key, '0') == '1' for key in ('paused', 'sleeping', 'restore_recovery_required'))):
                return False
            if (self.get_state('active_cycle_id', '') != cycle_id
                    or not self.connection.execute("SELECT 1 FROM cycles WHERE cycle_id=? AND session_id=? AND status='running'", (cycle_id, session_id)).fetchone()):
                return False
            pending = self.connection.execute("SELECT action_id FROM external_action_receipts WHERE cycle_id=? AND session_id=? AND state='uncertain'",
                                              (cycle_id, session_id)).fetchall()
            if not pending:
                return False
            # Every unfinished tool needs a supported receipt. Opaque code,
            # local storage failures and unrelated calls cannot be inferred safe.
            if self.connection.execute("SELECT 1 FROM cycle_tool_attempts t WHERE t.cycle_id=? AND t.outcome IN ('started','uncertain') "
                "AND NOT EXISTS(SELECT 1 FROM external_action_receipts r WHERE r.attempt_id=t.attempt_id "
                "AND r.adapter='gmail' AND t.tool_name='gmail.' || r.operation) LIMIT 1", (cycle_id,)).fetchone():
                return False
            hold = {'session_id': session_id, 'cycle_id': cycle_id, 'owner_revision': checkpoint['owner_revision'],
                    'action_ids': [row['action_id'] for row in pending]}
            self.connection.execute("UPDATE cycles SET status='stopped',ended_at=? WHERE cycle_id=? AND status='running'",
                                    (self.now(), cycle_id))
            for key, value in {'action_recovery_hold': json.dumps(hold), 'autonomy_error': 'action_outcome_uncertain',
                               'active_session_status': 'reconciling', 'session_start_requested': ''}.items():
                self.connection.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
            self.connection.execute("INSERT INTO activity(event,created_at) VALUES('Session held for read-only action reconciliation',?)", (self.now(),))
            return True

    @database_locked
    def action_hold_valid(self, session_id):
        try:
            hold = json.loads(self.get_state('action_recovery_hold', 'null'))
        except ValueError:
            return False
        return bool(isinstance(hold, dict) and hold.get('session_id') == session_id
            and isinstance(hold.get('cycle_id'), str) and bool(hold['cycle_id'])
            and isinstance(hold.get('action_ids'), list) and bool(hold['action_ids'])
            and all(isinstance(value, str) for value in hold['action_ids'])
            and self.get_state('active_session_id', '') == session_id
            and self.get_state('active_session_status', '') == 'reconciling'
            and self.get_state('autonomy_error', '') == 'action_outcome_uncertain'
            and hold.get('owner_revision') == self.get_state('owner_control_revision', '0'))

    @database_locked
    def release_confirmed_action_hold(self, session_id):
        with self.connection:
            if not self.action_hold_valid(session_id):
                return False
            hold = json.loads(self.get_state('action_recovery_hold'))
            checkpoint = self.connection.execute('SELECT k.phase FROM cycle_recovery_checkpoints k JOIN cycles c ON c.cycle_id=k.cycle_id WHERE c.cycle_id=? AND c.session_id=?', (hold['cycle_id'], session_id)).fetchone()
            if not checkpoint or checkpoint['phase'] != 'model_confirmed':
                return False
            checks = ('SELECT 1 FROM budget_reservations LIMIT 1', 'SELECT 1 FROM messages WHERE queued=2 LIMIT 1',
                'SELECT 1 FROM telegram_claims LIMIT 1', "SELECT 1 FROM telegram_outbox WHERE delivery_state='sending' LIMIT 1",
                'SELECT 1 FROM diary_delivery_claims LIMIT 1', "SELECT 1 FROM external_action_receipts WHERE state='uncertain' LIMIT 1",
                "SELECT 1 FROM cycles WHERE status='running' LIMIT 1",
                "SELECT 1 FROM cycle_tool_attempts WHERE cycle_id=? AND outcome IN ('started','uncertain') LIMIT 1")
            for query in checks:
                if self.connection.execute(query, (hold['cycle_id'],) if '?' in query else ()).fetchone():
                    return False
            if any(self.get_state(key, '0') == '1' for key in ('paused', 'sleeping', 'restore_recovery_required')):
                return False
            tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, condition in (('media_jobs', "state IN ('running','uncertain') OR delivery IN ('sending','uncertain')"),
                                     ('media_avatar_proposals', "state IN ('applying','uncertain')")):
                if table in tables and self.connection.execute(f'SELECT 1 FROM {table} WHERE {condition} LIMIT 1').fetchone():
                    return False
            receipts = []
            for action_id in hold['action_ids']:
                row = self.connection.execute("SELECT receipt FROM external_action_receipts WHERE action_id=? AND cycle_id=? AND session_id=? AND state='confirmed'", (action_id, hold['cycle_id'], session_id)).fetchone()
                if row is None:
                    return False
                receipts.append({'action_id': action_id, **json.loads(row['receipt'])})
            for key, value in {'autonomy_error': '', 'active_session_status': 'running', 'action_recovery_hold': '',
                    'session_recovery_checkpoint': json.dumps({'cycle_id': hold['cycle_id'], 'phase': 'model_confirmed', 'verified_actions': receipts})}.items():
                self.connection.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
            self.connection.execute("UPDATE cycles SET status='complete' WHERE cycle_id=?", (hold['cycle_id'],))
            self.connection.execute("DELETE FROM state WHERE key='active_cycle_id'")
            self.connection.execute("INSERT INTO activity(event,created_at) VALUES('Session resumed after verified action reconciliation; no action replayed',?)", (self.now(),))
            return True

    @database_locked
    def hold_pending_session_actions(self, session_id):
        row = self.connection.execute(
            "SELECT cycle_id FROM cycles WHERE session_id=? AND status='running' ORDER BY rowid DESC LIMIT 1",
            (session_id,)).fetchone()
        return bool(row and self.hold_confirmed_actions(session_id, row['cycle_id']))

    @database_locked
    def recover_confirmed_session(self, session_id):
        """Resume only a proven boundary; never infer success from absent errors.

        Returns the recovered phase, 'idle', or None. No provider/tool/Telegram
        operation is replayed, and no uncertain record or reservation is cleared.
        Caller must invoke this before starting dispatch workers.
        """
        with self.connection:
            if (not session_id or self.get_state('active_session_id', '') != session_id
                    or self.get_state('active_session_status', '') != 'running'
                    or self.get_state('autonomy_error', '')
                    or any(self.get_state(key, '0') == '1' for key in
                           ('paused', 'sleeping', 'restore_recovery_required'))):
                return None
            # Check before startup quarantines in-flight claims: otherwise a
            # quarantined operation could look like an ordinary idle restart.
            checks = (
                "SELECT 1 FROM external_action_receipts WHERE state='uncertain' LIMIT 1",
                'SELECT 1 FROM budget_reservations LIMIT 1',
                'SELECT 1 FROM messages WHERE queued=2 LIMIT 1',
                'SELECT 1 FROM telegram_claims LIMIT 1',
                "SELECT 1 FROM telegram_outbox WHERE delivery_state='sending' LIMIT 1",
                'SELECT 1 FROM diary_delivery_claims LIMIT 1',
                "SELECT 1 FROM cycles WHERE status='running' AND session_id<>? LIMIT 1",
            )
            for query in checks:
                if self.connection.execute(query, (session_id,) if '?' in query else ()).fetchone():
                    return None
            tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, condition in (
                ('media_jobs', "state IN ('running','uncertain') OR delivery IN ('sending','uncertain')"),
                ('media_avatar_proposals', "state IN ('applying','uncertain')"),
            ):
                if table in tables and self.connection.execute(f'SELECT 1 FROM {table} WHERE {condition} LIMIT 1').fetchone():
                    return None
            cycles = self.connection.execute(
                "SELECT c.cycle_id,k.phase,k.owner_revision,k.research_id,k.research_revision,k.activity_id "
                'FROM cycles c LEFT JOIN cycle_recovery_checkpoints k ON k.cycle_id=c.cycle_id '
                "WHERE c.session_id=? AND c.status='running'", (session_id,),
            ).fetchall()
            if not cycles:
                last = self.connection.execute(
                    'SELECT status FROM cycles WHERE session_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1',
                    (session_id,),
                ).fetchone()
                # A crash can land between marking the cycle failed and
                # writing the session error. That is not an idle session.
                if last and last['status'] in {'failed', 'interrupted'}:
                    return None
                return 'idle'
            if len(cycles) != 1:
                return None
            row = cycles[0]
            if (row['phase'] not in {'prepared', 'model_confirmed'}
                    or self.get_state('active_cycle_id', '') != row['cycle_id']
                    or row['owner_revision'] != self.get_state('owner_control_revision', '0')):
                return None
            if self.connection.execute(
                "SELECT 1 FROM cycle_tool_attempts WHERE cycle_id=? AND (outcome IN ('started','uncertain') OR (?='prepared' AND outcome<>'not_executed')) LIMIT 1",
                (row['cycle_id'], row['phase']),
            ).fetchone():
                return None
            status = 'complete' if row['phase'] == 'model_confirmed' else 'stopped'
            self.connection.execute('UPDATE cycles SET status=?,ended_at=? WHERE cycle_id=?',
                                    (status, self.now(), row['cycle_id']))
            self.connection.execute("DELETE FROM state WHERE key='active_cycle_id'")
            receipt = {key: row[key] for key in row.keys() if key != 'owner_revision'}
            self.connection.execute("INSERT INTO state(key,value) VALUES('session_recovery_checkpoint',?) "
                                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (json.dumps(receipt),))
            self.connection.execute('INSERT INTO activity(event,created_at) VALUES(?,?)',
                (f"Autonomous session recovered from confirmed checkpoint (phase={row['phase']})", self.now()))
            self._research_event('cycle_finished', outcome=status, session_id=session_id, cycle_id=row['cycle_id'])
            return row['phase']

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
                if status == 'complete':
                    self.connection.execute("DELETE FROM state WHERE key='provider_retry_streak'")
            return cursor.rowcount == 1

    PROVIDER_RETRY_DELAYS = (60, 120, 240, 480, 960, 1800)
    _RESTORE_FENCES = ('restore_recovery_required', 'local_recovery_restore_required')

    @database_locked
    def record_provider_attempt(self, reservation_id):
        """Attribute one legacy gateway dispatch to the running autonomous cycle, if any."""
        with self.connection:
            cycle_id = self.get_state('active_cycle_id', '')
            session_id = self.get_state('active_session_id', '')
            if not (cycle_id and session_id and self.get_state('autonomous_owner_id', '')):
                return None
            return self.connection.execute(
                "INSERT INTO provider_attempts(session_id,cycle_id,reservation_id,outcome,created_at) "
                "VALUES(?,?,?,'dispatching',?)", (session_id, cycle_id, reservation_id, self.now())).lastrowid

    @database_locked
    def finish_provider_attempt(self, attempt_id, outcome):
        if attempt_id is None:
            return
        if outcome not in {'not_reached', 'completed', 'uncertain'}:
            raise ValueError('invalid provider attempt outcome')
        with self.connection:
            self.connection.execute(
                "UPDATE provider_attempts SET outcome=?,finished_at=? WHERE attempt_id=? AND outcome='dispatching'",
                (outcome, self.now(), attempt_id))

    def _unsent_cycle_proven(self, session_id, cycle_id, owner_id):
        """Caller holds the lock and transaction. Every check must pass, or the normal stop applies."""
        state = dict(self.connection.execute('SELECT key,value FROM state').fetchall())
        if (state.get('active_session_id', '') != session_id or state.get('active_cycle_id', '') != cycle_id
                or state.get('autonomy_error', '') or state.get('paused', '0') == '1' or state.get('sleeping', '0') == '1'
                or any(state.get(key, '0') == '1' for key in self._RESTORE_FENCES)):
            return False
        cycle = self.connection.execute("SELECT 1 FROM cycles WHERE cycle_id=? AND session_id=? AND status='running'",
                                        (cycle_id, session_id)).fetchone()
        checkpoint = self.connection.execute('SELECT owner_revision FROM cycle_recovery_checkpoints WHERE cycle_id=?',
                                             (cycle_id,)).fetchone()
        if not cycle or not checkpoint or checkpoint[0] != state.get('owner_control_revision', '0'):
            return False
        attempts = self.connection.execute('SELECT session_id,reservation_id,outcome FROM provider_attempts WHERE cycle_id=?',
                                           (cycle_id,)).fetchall()
        if not attempts or any(row[0] != session_id or row[2] != 'not_reached' for row in attempts):
            return False
        if self.connection.execute(
                'SELECT 1 FROM budget_reservations WHERE reservation_id IN (SELECT reservation_id FROM provider_attempts WHERE cycle_id=?) LIMIT 1',
                (cycle_id,)).fetchone():
            return False
        if self.connection.execute('SELECT 1 FROM cycle_tool_attempts WHERE cycle_id=? LIMIT 1', (cycle_id,)).fetchone():
            return False
        if self.connection.execute("SELECT 1 FROM model_turn_receipts WHERE cycle_id=? AND state='completed'", (cycle_id,)).fetchone():
            return False
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_attempts'").fetchone() and \
                self.connection.execute("SELECT 1 FROM recovery_attempts WHERE json_extract(binding_json,'$.session_id')=? LIMIT 1",
                                        (session_id,)).fetchone():
            return False
        return not self.connection.execute(
            "SELECT 1 FROM messages WHERE owner_id=? AND role='user' AND queued=3 LIMIT 1", (owner_id,)).fetchone()

    def _put_state(self, updates):
        for key, value in updates.items():
            self.connection.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                    (key, str(value)))

    @database_locked
    def schedule_unsent_provider_retry(self, session_id, cycle_id, owner_id, now):
        """End a cycle whose every provider attempt is proven unsent and schedule a backoff restart.

        Returns the retry time, or None when the proof is incomplete or the backoff is exhausted.
        """
        with self.connection:
            if not self._unsent_cycle_proven(session_id, cycle_id, owner_id):
                return None
            streak = int(self.get_state('provider_retry_streak', '0') or '0')
            if streak >= len(self.PROVIDER_RETRY_DELAYS):
                return None
            retry_at = now.astimezone(timezone.utc) + timedelta(seconds=self.PROVIDER_RETRY_DELAYS[streak])
            self.connection.execute("UPDATE cycles SET status='failed',ended_at=? WHERE cycle_id=? AND status='running'",
                                    (self.now(), cycle_id))
            self._research_event('cycle_finished', outcome='failed', session_id=session_id, cycle_id=cycle_id)
            self.connection.execute("DELETE FROM state WHERE key='active_cycle_id' AND value=?", (cycle_id,))
            self._put_state({'active_session_id': '', 'active_session_started_at': '',
                             'active_session_status': 'provider_unreachable',
                             'session_start_requested': 'provider_retry',
                             'provider_retry_at': retry_at.isoformat(timespec='seconds'),
                             'provider_retry_owner_revision': self.get_state('owner_control_revision', '0'),
                             'provider_retry_streak': str(streak + 1)})
            self.connection.execute('INSERT INTO activity(event,created_at) VALUES(?,?)', (
                f'Provider unreachable before any request was sent; autonomous retry {streak + 1} scheduled', self.now()))
            return retry_at

    @database_locked
    def start_provider_retry_session(self, session_id, now, owner_id):
        """Atomically start a due provider retry, or cancel it if the owner or lifecycle changed."""
        with self.connection:
            state = dict(self.connection.execute('SELECT key,value FROM state').fetchall())
            if state.get('session_start_requested', '') != 'provider_retry':
                return False
            try:
                retry_at = datetime.fromisoformat(state.get('provider_retry_at', ''))
                valid = retry_at.tzinfo is not None
            except ValueError:
                valid = False
            if valid and now < retry_at:
                return False
            if (not valid or state.get('owner_control_revision', '0') != state.get('provider_retry_owner_revision')
                    or state.get('paused', '0') == '1' or state.get('sleeping', '0') == '1'
                    or state.get('autonomy_error', '') or state.get('active_session_id', '')
                    or any(state.get(key, '0') == '1' for key in self._RESTORE_FENCES)):
                self._put_state({'session_start_requested': '', 'provider_retry_at': ''})
                self.connection.execute('INSERT INTO activity(event,created_at) VALUES(?,?)', (
                    'Scheduled provider retry cancelled by an owner control or lifecycle change', self.now()))
                return False
            if self.pending_owner_input(owner_id):
                return False
            self._put_state({'active_session_id': session_id,
                             'active_session_started_at': now.astimezone(timezone.utc).isoformat(timespec='seconds'),
                             'active_session_status': 'running', 'session_start_requested': '',
                             'session_recovery_checkpoint': '', 'provider_retry_at': ''})
            self.connection.execute('INSERT INTO activity(event,created_at) VALUES(?,?)',
                                    ('Autonomous session started (provider_retry)', self.now()))
            return True

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
    def pending_owner_controls(self, owner_id):
        prompt_after = (int(self.get_state('telegram_wake_prompt_after_update_id', '-1'))
                        if self.get_state('telegram_wake_prompt_until', '') else None)
        rows = self.connection.execute(
            "SELECT update_id,payload FROM telegram_inbox "
            "WHERE andrew_owner_control(payload,?,?) "
            "AND update_id NOT IN (SELECT update_id FROM telegram_claims) "
            "ORDER BY update_id LIMIT 50", (owner_id, prompt_after)).fetchall()
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
        self.apply_telegram_receipts('reply', row['id'], row['chat_id'])
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
                "INSERT OR IGNORE INTO telegram_delivery_progress(kind,record_id,content,chunk_attempts,delivery_id) VALUES(?,?,?,?,?)",
                (kind, str(record_id), content, int(previously_attempted), uuid.uuid4().hex))
            return dict(self.connection.execute(
                "SELECT * FROM telegram_delivery_progress WHERE kind=? AND record_id=?",
                (kind, str(record_id))).fetchone())

    @database_locked
    def attempt_telegram_chunk(self, kind, record_id, chunk):
        with self.connection:
            rows = self.connection.execute(
                "UPDATE telegram_delivery_progress SET chunk_attempts=1,chunk_token=? "
                "WHERE kind=? AND record_id=? AND next_chunk=? AND chunk_attempts=0 AND NOT EXISTS (SELECT 1 FROM telegram_chunk_receipts r WHERE r.delivery_id=telegram_delivery_progress.delivery_id AND r.chunk=telegram_delivery_progress.next_chunk) RETURNING retry_notice",
                (uuid.uuid4().hex, kind, str(record_id), chunk)).fetchall()
            if not rows:
                return None
            if kind == 'reply':
                self.connection.execute('UPDATE telegram_outbox SET attempts=attempts+1 WHERE id=?', (record_id,))
            return rows[0]['retry_notice']

    @database_locked
    def acknowledge_telegram_chunk(self, kind, record_id, chunk):
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE telegram_delivery_progress SET next_chunk=next_chunk+1,chunk_attempts=0,retry_notice=0,chunk_token='' "
                "WHERE kind=? AND record_id=? AND next_chunk=?",
                (kind, str(record_id), chunk))
            if cursor.rowcount != 1:
                raise ValueError('Telegram delivery progress changed')

    @database_locked
    def telegram_chunk_context(self, kind, record_id, chunk):
        row = self.connection.execute('SELECT * FROM telegram_delivery_progress WHERE kind=? AND record_id=? AND next_chunk=? AND chunk_attempts=1',
                                      (kind, str(record_id), chunk)).fetchone()
        if row is None or not row['delivery_id'] or not row['chunk_token']:
            raise ValueError('Telegram part has no current send admission')
        return dict(row)

    @database_locked
    def save_telegram_receipt(self, context, chat_id, wire, result):
        from .telegram import validate_sent_text, split_message, RETRY_NOTICE
        message_id = validate_sent_text(result, chat_id, wire)
        expected = (RETRY_NOTICE if context['retry_notice'] else '') + split_message(context['content'])[context['next_chunk']]
        if wire != expected:
            raise ValueError('Telegram confirmation does not match the admitted part')
        with self.connection:
            row = self.connection.execute('SELECT 1 FROM telegram_delivery_progress WHERE kind=? AND record_id=? AND delivery_id=? AND chunk_token=? AND next_chunk=? AND chunk_attempts=1',
                (context['kind'], context['record_id'], context['delivery_id'], context['chunk_token'], context['next_chunk'])).fetchone()
            if row is None:
                raise ValueError('Telegram send admission changed before confirmation')
            self.connection.execute('INSERT INTO telegram_chunk_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',
                (context['chunk_token'], context['delivery_id'], context['kind'], context['record_id'], context['next_chunk'],
                 chat_id, message_id, hashlib.sha256(wire.encode()).hexdigest(), hashlib.sha256(context['content'].encode()).hexdigest(), self.now()))

    @database_locked
    def apply_telegram_receipts(self, kind, record_id, chat_id):
        """Advance confirmed parts only; never make a Telegram request."""
        from .telegram import split_message, RETRY_NOTICE
        applied = 0
        with self.connection:
            while True:
                row = self.connection.execute('SELECT * FROM telegram_delivery_progress WHERE kind=? AND record_id=?', (kind, str(record_id))).fetchone()
                if row is None or not row['chunk_attempts'] or not row['chunk_token']:
                    return applied
                receipt = self.connection.execute('SELECT * FROM telegram_chunk_receipts WHERE attempt_id=? AND delivery_id=? AND kind=? AND record_id=? AND chunk=? AND chat_id=?',
                    (row['chunk_token'], row['delivery_id'], kind, str(record_id), row['next_chunk'], chat_id)).fetchone()
                if receipt is None or receipt['content_sha256'] != hashlib.sha256(row['content'].encode()).hexdigest():
                    return applied
                chunks = split_message(row['content'])
                if not 0 <= row['next_chunk'] < len(chunks):
                    return applied
                wire = (RETRY_NOTICE if row['retry_notice'] else '') + chunks[row['next_chunk']]
                if hashlib.sha256(wire.encode()).hexdigest() != receipt['wire_sha256']:
                    return applied
                self.acknowledge_telegram_chunk(kind, record_id, row['next_chunk'])
                applied += 1

    def _has_telegram_receipt(self, kind, record_id, chat_id):
        # Caller holds the Store lock; old record IDs and changed bodies cannot
        # borrow another delivery's proof.
        row = self.connection.execute('SELECT delivery_id,content FROM telegram_delivery_progress WHERE kind=? AND record_id=?', (kind, str(record_id))).fetchone()
        return bool(row and self.connection.execute('SELECT 1 FROM telegram_chunk_receipts WHERE delivery_id=? AND chat_id=? AND content_sha256=? LIMIT 1',
            (row['delivery_id'], chat_id, hashlib.sha256(row['content'].encode()).hexdigest())).fetchone())

    @database_locked
    def reconcile_telegram_receipts(self, owner_id, startup=False):
        """Settle proved deliveries; active senders keep their claims at runtime."""
        from .telegram import split_message
        settled = 0
        with self.connection:
            query = 'SELECT * FROM telegram_outbox' + ('' if startup else " WHERE delivery_state='held'")
            for row in self.connection.execute(query).fetchall():
                applied = self.apply_telegram_receipts('reply', row['id'], row['chat_id'])
                if not applied and not self._has_telegram_receipt('reply', row['id'], row['chat_id']):
                    continue
                status = self._reply_status(row)
                if status == 'held':
                    continue
                if status == 'complete':
                    self.connection.execute('DELETE FROM telegram_outbox WHERE id=?', (row['id'],))
                    self._delete_reply_progress(row['id'])
                elif status == 'pending':
                    self.connection.execute("UPDATE telegram_outbox SET delivery_state='pending',claim_token=NULL WHERE id=?", (row['id'],))
                settled += 1
            for row in self.connection.execute("SELECT p.* FROM telegram_delivery_progress p JOIN diary_entries d ON d.entry_id=p.record_id WHERE p.kind='diary' AND d.delivery_state='uncertain'").fetchall():
                if not startup and self.connection.execute('SELECT 1 FROM diary_delivery_claims WHERE entry_id=?', (row['record_id'],)).fetchone():
                    continue
                if (not self.apply_telegram_receipts('diary', row['record_id'], owner_id)
                        and not self._has_telegram_receipt('diary', row['record_id'], owner_id)):
                    continue
                progress = self.connection.execute("SELECT * FROM telegram_delivery_progress WHERE kind='diary' AND record_id=?", (row['record_id'],)).fetchone()
                if progress['chunk_attempts']:
                    continue
                self.connection.execute('DELETE FROM diary_delivery_claims WHERE entry_id=?', (row['record_id'],))
                if progress['next_chunk'] >= len(split_message(progress['content'])):
                    self.mark_diary_delivered(row['record_id'])
                else:
                    self.connection.execute("UPDATE diary_entries SET delivery_state='pending' WHERE entry_id=?", (row['record_id'],))
                settled += 1
            if settled:
                self.connection.execute('INSERT INTO activity(event,created_at) VALUES(?,?)',
                    ('Telegram delivery recovered from confirmed part receipts; no confirmed part resent', self.now()))
        return settled

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
            # A positive receipt must be consumed before considering any retry.
            if self.connection.execute("SELECT 1 FROM telegram_delivery_progress p JOIN telegram_chunk_receipts r ON r.attempt_id=p.chunk_token AND r.delivery_id=p.delivery_id AND r.chunk=p.next_chunk WHERE p.kind='diary' AND p.record_id=?", (entry_id,)).fetchone():
                self.connection.execute("UPDATE diary_entries SET delivery_state='pending' WHERE entry_id=?", (entry_id,))
                return True
            content = format_diary_entry(self._diary_row(row))
            self.connection.execute(
                "INSERT OR IGNORE INTO telegram_delivery_progress(kind,record_id,content,delivery_id) VALUES('diary',?,?,?)",
                (entry_id, content, uuid.uuid4().hex))
            self.connection.execute("UPDATE telegram_delivery_progress SET chunk_attempts=0,chunk_token='',retry_notice=1 WHERE kind='diary' AND record_id=?", (entry_id,))
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
    def create_memory_record(self, record_type, content, source, event_ids=(), diary_ids=(), *,
                             history_ids=(), owner_id=None, untrusted=False):
        if record_type not in {"experience", "interest"}:
            raise ValueError("invalid memory record type")
        if not isinstance(content, str) or not content.strip() or len(content) > 4000:
            raise ValueError("invalid memory content")
        if source not in {"andrew", "owner", "external", "system"}:
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
            self._create_memory_provenance(record_id, source, history_ids, owner_id, untrusted)
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
                self._create_memory_provenance(record_id, 'owner', (), None, False)
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
    def create_personality_revision(self, content, reason, source, event_ids=(), diary_ids=(), *,
                                    history_ids=(), owner_id=None, untrusted=False):
        if not isinstance(content, str) or not content.strip() or len(content) > 4000:
            raise ValueError("invalid personality content")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("invalid personality revision reason")
        if source not in {"andrew", "owner", "external", "system"}:
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
            self._create_memory_provenance(revision_id, source, history_ids, owner_id, untrusted)
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

    def _create_memory_provenance(self, record_id, source, history_ids, owner_id, untrusted):
        history_ids = self._validate_reference_ids(history_ids, int)
        origin = {'owner': 'owner', 'andrew': 'agent', 'external': 'untrusted', 'system': 'system'}[source]
        basis = 'controller' if source in {'owner', 'system'} else 'authored'
        if history_ids:
            if type(owner_id) is not int:
                raise ValueError('history references require an authenticated owner')
            marks = ','.join('?' for _ in history_ids)
            rows = self.connection.execute(
                f'SELECT id,role,complete FROM session_history WHERE owner_id=? AND id IN ({marks})',
                (owner_id, *history_ids)).fetchall()
            if {row['id'] for row in rows} != set(history_ids):
                raise ValueError('unknown history reference')
            if any(row['role'] != 'user' or not row['complete'] for row in rows):
                untrusted = True
            basis = 'history_linked'
        if untrusted or source == 'external':
            origin = 'untrusted'
            basis = 'external_context' if untrusted and not history_ids else 'external_source'
        self.connection.execute('INSERT INTO memory_provenance VALUES(?,?,?,?)',
                                (record_id, origin, basis, self.now()))
        self.connection.executemany('INSERT INTO memory_history_refs VALUES(?,?)',
                                    [(record_id, history_id) for history_id in history_ids])

    def _memory_provenance(self, record_id):
        row = self.connection.execute('SELECT origin,basis FROM memory_provenance WHERE record_id=?', (record_id,)).fetchone()
        return {'provenance': row['origin'] if row else 'untrusted',
                'provenance_basis': row['basis'] if row else 'legacy_unverified',
                'history_ids': [ref[0] for ref in self.connection.execute(
                    'SELECT history_id FROM memory_history_refs WHERE record_id=? ORDER BY history_id', (record_id,))]}

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
            "SELECT r.* FROM personality_revisions r JOIN memory_provenance p ON p.record_id=r.revision_id "
            "WHERE p.origin IN ('owner','agent','system') ORDER BY r.created_at DESC,r.rowid DESC LIMIT 1"
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
        result.update(self._memory_provenance(result['revision_id']))
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
            result.update(self._memory_provenance(record_id))
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
            result.update(self._memory_provenance(result['record_id']))
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


    @database_locked
    def record_runner_research_result(self, attempt_id, context, *, outcome):
        """Record saved linkage inside the caller's atomic receipt import."""
        if not self.connection.in_transaction or outcome!='returned':
            raise ValueError('Runner research transaction required')
        row=self.connection.execute('SELECT * FROM cycle_tool_attempts WHERE attempt_id=?',(attempt_id,)).fetchone()
        if row is None or row['cycle_id']!=context['cycle_id'] or row['outcome']!='returned':
            raise ValueError('Runner research identity mismatch')
        from .recovery_identity import canonical
        encoded=canonical(context)
        old=self.connection.execute('SELECT context_json FROM runner_research_results WHERE attempt_id=?',(attempt_id,)).fetchone()
        if old is not None:
            if old['context_json']!=encoded:raise ValueError('Runner research identity mismatch')
            return
        self._research_event('tool_used',context['research_id'],row['tool_name'],outcome,
                             session_id=context['session_id'],cycle_id=context['cycle_id'])
        event_id=self.connection.execute('SELECT last_insert_rowid()').fetchone()[0]
        self.connection.execute('INSERT INTO runner_research_results VALUES(?,?,?)',(attempt_id,encoded,event_id))
