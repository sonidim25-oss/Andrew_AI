import sqlite3
import threading
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps
from zoneinfo import ZoneInfo


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
        """)
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
    def claim_queued_messages(self, owner_id):
        with self.connection:
            rows = self.connection.execute(
                "SELECT id,content,created_at FROM messages WHERE owner_id=? AND role='user' AND queued=1 ORDER BY id",
                (owner_id,),
            ).fetchall()
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
        cursor = self.connection.execute(
            "UPDATE messages SET queued=1 WHERE owner_id=? AND role='user' AND queued=3", (owner_id,)
        )
        self.connection.commit()
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
    def transition_state(self, updates, event):
        if not isinstance(updates, dict) or not isinstance(event, str) or not event:
            raise ValueError("state transition requires updates and an event")
        with self.connection:
            for key, value in updates.items():
                self.connection.execute(
                    "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(key), str(value)),
                )
            self.connection.execute(
                "INSERT INTO activity(event,created_at) VALUES(?,?)", (event, self.now())
            )

    @database_locked
    def process_fallback_wake(self, local_day, owner_id):
        """Record one daily checkpoint and wake only an indefinite sleep."""
        with self.connection:
            state = dict(self.connection.execute(
                "SELECT key,value FROM state WHERE key IN ('sleeping','sleep_until','fallback_wake_day')"
            ).fetchall())
            if state.get("fallback_wake_day", "") == local_day:
                return False
            asleep_indefinitely = state.get("sleeping", "0") == "1" and not state.get("sleep_until", "")
            updates = {"fallback_wake_day": local_day}
            if asleep_indefinitely:
                updates.update({"sleeping": "0", "sleep_until": "", "session_start_requested": "fallback_wake"})
            for key, value in updates.items():
                self.connection.execute(
                    "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            if asleep_indefinitely:
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
            return asleep_indefinitely

    @database_locked
    def begin_cycle(self, session_id, cycle_id, local_day, reserved_cost_usd=0.0):
        self.connection.execute(
            "INSERT INTO cycles(cycle_id,session_id,local_day,status,started_at,reserved_cost_usd) VALUES(?,?,?,?,?,?)",
            (cycle_id, session_id, local_day, "running", self.now(), max(0.0, float(reserved_cost_usd))),
        )
        self.connection.commit()

    @database_locked
    def finish_cycle(self, cycle_id, status, actual_cost_usd=None):
        if status not in {"complete", "failed", "interrupted", "stopped"}:
            raise ValueError("invalid cycle status")
        cursor = self.connection.execute(
            "UPDATE cycles SET status=?,ended_at=?,actual_cost_usd=? WHERE cycle_id=? AND status='running'",
            (status, self.now(), None if actual_cost_usd is None else max(0.0, float(actual_cost_usd)), cycle_id),
        )
        self.connection.commit()
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
            "SELECT update_id,payload FROM telegram_inbox ORDER BY update_id"
        ).fetchall()
        return [(row["update_id"], json.loads(row["payload"])) for row in rows]

    @database_locked
    def complete_update(self, update_id):
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO updates(update_id) VALUES(?)", (update_id,))
            self.connection.execute("DELETE FROM telegram_inbox WHERE update_id=?", (update_id,))

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
        self.connection.execute(
            "INSERT INTO budget_reservations(reservation_id,month,day,cost_usd,created_at) VALUES(?,?,?,?,?)",
            (reservation_id, month, day, max(0.0, float(cost_usd)), self.now()),
        )
        self.connection.commit()

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
    def mark_diary_delivered(self, entry_id):
        cursor = self.connection.execute(
            "UPDATE diary_entries SET delivery_state='delivered' WHERE entry_id=? AND delivery_state='uncertain'",
            (entry_id,),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    @database_locked
    def retry_uncertain_diary_delivery(self, entry_id):
        cursor = self.connection.execute(
            "UPDATE diary_entries SET delivery_state='pending' WHERE entry_id=? AND delivery_state='uncertain'",
            (entry_id,),
        )
        self.connection.commit()
        return cursor.rowcount == 1

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
