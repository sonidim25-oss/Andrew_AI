"""Trusted, durable owner-only media queue. Ambiguous attempts never replay."""
import hashlib
import json
import math
import os
import re
import threading
import uuid
from dataclasses import dataclass
from contextvars import ContextVar

MEDIA_AUTHORIZATION = ContextVar("media_owner_authorization", default=None)
from datetime import datetime, timezone
from pathlib import Path
from .tool_errors import ToolRejected
from .telegram import TelegramMediaError, validate_sent_media


def validate_png(data):
    import struct
    import zlib
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return False
    position, compressed, header, ended = 8, [], None, False
    try:
        while position < len(data):
            size = struct.unpack('>I', data[position:position + 4])[0]
            kind = data[position + 4:position + 8]
            body = data[position + 8:position + 8 + size]
            crc = struct.unpack('>I', data[position + 8 + size:position + 12 + size])[0]
            if zlib.crc32(kind + body) != crc:
                return False
            if header is None and kind != b'IHDR':
                return False
            if kind == b'IHDR':
                if header is not None or size != 13:
                    return False
                header = struct.unpack('>IIBBBBB', body)
            elif kind == b'IDAT':
                compressed.append(body)
            elif kind == b'IEND':
                ended = size == 0 and position + 12 == len(data)
                break
            position += 12 + size
        if not ended or not header:
            return False
        width, height, bits, color, compression, filtering, interlace = header
        if not (1 <= width <= 2048 and 1 <= height <= 2048 and bits == 8
                and color in {2, 6} and compression == filtering == interlace == 0):
            return False
        expected = height * (1 + width * (3 if color == 2 else 4))
        decoder = zlib.decompressobj()
        raw = decoder.decompress(b''.join(compressed), expected + 1)
        return len(raw) == expected and decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail
    except (ValueError, struct.error, zlib.error):
        return False


@dataclass(frozen=True)
class MediaPolicy:
    enabled: bool = False
    voice_enabled: bool = True
    spontaneous_voice: bool = False
    spontaneous_image: bool = False
    voice: str = 'george'
    tts_model: str = 'elevenlabs/eleven-v3'
    image_model: str = 'openai/gpt-image-1-mini'
    # Legacy amount fields remain configuration-compatible, never spending caps.
    monthly_usd: float = 3.0
    daily_usd: float = .20
    voice_job_usd: float = .08
    image_job_usd: float = .10
    voice_character_usd: float = .00008  # regular list rate, not temporary discount
    retention_days: int = 30

    def __post_init__(self):
        for value in (self.monthly_usd, self.daily_usd, self.voice_job_usd,
                      self.image_job_usd, self.voice_character_usd):
            if not math.isfinite(value) or value <= 0:
                raise ValueError('Media budgets must be positive finite amounts')
        if self.tts_model != 'elevenlabs/eleven-v3' or self.voice not in {
                'george', 'brian', 'daniel', 'adam', 'sarah', 'alice', 'jessica'}:
            raise ValueError('Select Eleven v3 and an approved premade voice')
        if self.image_model != 'openai/gpt-image-1-mini' or self.retention_days < 1:
            raise ValueError('Unsupported media configuration')


def load_media_policy(environment):
    enabled = environment.get('ANDREW_MEDIA_ENABLED', '0') == '1'
    if enabled and environment.get('ANDREW_MEDIA_CAPABILITIES_VERIFIED', '0') != '1':
        raise ValueError('Media requires verified provider capabilities before enabling')
    try:
        return MediaPolicy(enabled=enabled, voice_enabled=environment.get('ANDREW_MEDIA_VOICE_ENABLED', '1') == '1',
                           spontaneous_voice=environment.get('ANDREW_MEDIA_SPONTANEOUS_VOICE', '0') == '1',
                           spontaneous_image=environment.get('ANDREW_MEDIA_SPONTANEOUS_IMAGE', '0') == '1',
                           voice=environment.get('ANDREW_TTS_VOICE', 'george'),
                           monthly_usd=float(environment.get('ANDREW_MEDIA_MONTHLY_PER_KIND_USD', '3')),
                           daily_usd=float(environment.get('ANDREW_MEDIA_DAILY_PER_KIND_USD', '.20')),
                           voice_job_usd=float(environment.get('ANDREW_MEDIA_VOICE_JOB_USD', '.08')),
                           image_job_usd=float(environment.get('ANDREW_MEDIA_IMAGE_JOB_USD', '.10')),
                           voice_character_usd=float(environment.get('ANDREW_MEDIA_VOICE_CHARACTER_USD', '.00008')),
                           retention_days=int(environment.get('ANDREW_MEDIA_RETENTION_DAYS', '30')))
    except (TypeError, ValueError):
        raise ValueError('Invalid media policy configuration') from None


class MediaService:
    def __init__(self, store, artifacts, owner_id, policy=None):
        self.store, self.owner_id = store, int(owner_id)
        self.policy = policy or MediaPolicy()
        self.artifacts = Path(artifacts)
        self.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifacts.chmod(0o700)
        with store._lock, store.connection:
            store.connection.executescript('''
                CREATE TABLE IF NOT EXISTS media_jobs (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                    state TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'pending',
                    reserved_usd REAL NOT NULL, actual_usd REAL,
                    mime TEXT, artifact_sha TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, error TEXT, telegram_message_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS media_avatar_proposals (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES media_jobs(id),
                    state TEXT NOT NULL DEFAULT 'proposed', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS media_delivery_receipts (
                    attempt_id TEXT PRIMARY KEY, job_id TEXT UNIQUE NOT NULL REFERENCES media_jobs(id),
                    chat_id INTEGER NOT NULL, kind TEXT NOT NULL, mime TEXT NOT NULL,
                    artifact_sha TEXT NOT NULL, job_digest TEXT NOT NULL,
                    message_id INTEGER NOT NULL, file_identity_sha TEXT NOT NULL, confirmed_at TEXT NOT NULL
                );
            ''')
            store.connection.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in store.connection.execute('PRAGMA table_info(media_jobs)')}
            for name, definition in (('delivery_attempt', "TEXT NOT NULL DEFAULT ''"), ('delivery_chat_id', 'INTEGER')):
                if name not in columns:
                    store.connection.execute('ALTER TABLE media_jobs ADD COLUMN ' + name + ' ' + definition)

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def _usage(self, kind, period):
        row = self.store.connection.execute('''SELECT coalesce(sum(CASE
            WHEN state='succeeded' THEN coalesce(actual_usd,reserved_usd)
            WHEN state IN ('queued','running','uncertain') THEN reserved_usd ELSE 0 END),0)
            FROM media_jobs WHERE kind=? AND created_at LIKE ?''', (kind, period + '%')).fetchone()
        return float(row[0])

    def spontaneous_enabled(self, kind):
        if not self.policy.enabled:
            return False
        if kind == 'voice':
            return (self.policy.voice_enabled and self.policy.spontaneous_voice
                    and self.store.get_state('media_spontaneous_voice', '1') == '1')
        return kind == 'image' and self.policy.spontaneous_image

    def submit(self, kind, text, request_key, *, owner_requested=False, voice_id=None, reference_id=None):
        if self.store.get_state('restore_recovery_required', '0') == '1':
            raise ToolRejected('Restore requires billing and action reconciliation')
        if not self.policy.enabled or (kind == 'voice' and not self.policy.voice_enabled):
            raise ToolRejected('Media generation is disabled')
        if not owner_requested and (self.store.get_state('paused', '0') == '1' or self.store.get_state('sleeping', '0') == '1'):
            raise ToolRejected('Spontaneous generation is paused or sleeping')
        if kind not in {'voice', 'image'} or not (owner_requested or self.spontaneous_enabled(kind)):
            raise ToolRejected('Media requires an owner request or standing permission')
        maximum = 1000 if kind == 'voice' else 2000
        if not isinstance(text, str) or not text.strip() or len(text) > maximum:
            raise ToolRejected('Media input is empty or too long')
        if not isinstance(request_key, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}', request_key):
            raise ToolRejected('Invalid media request ID')
        if voice_id is not None and voice_id != self.policy.voice:
            raise ToolRejected('Only the configured voice is approved')
        if reference_id is not None:
            if kind != 'image' or not isinstance(reference_id, str) or not re.fullmatch(r'[a-f0-9]{32}', reference_id):
                raise ToolRejected('Invalid image edit artifact')
        # The controller never accepts arbitrary URLs, paths, recipients, or provider settings.
        payload = json.dumps({'text': text, 'voice': self.policy.voice,
                              'model': self.policy.tts_model if kind == 'voice' else self.policy.image_model,
                              'reference_id': reference_id, 'origin': 'owner' if owner_requested else 'agent'},
                             ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256((kind + payload).encode()).hexdigest()
        reserve = len(text) * self.policy.voice_character_usd if kind == 'voice' else self.policy.image_job_usd
        now = self.now()
        with self.store._lock, self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            old = self.store.connection.execute('SELECT id,digest FROM media_jobs WHERE request_key=?', (request_key,)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ToolRejected('Media request ID was already used with different arguments')
                return old['id']
            if reference_id is not None:
                # Cleanup must not remove the source between validation and
                # insertion of the durable reference, including other stores.
                original = self.job(reference_id, preflight=True)
                if original['kind'] != 'image' or original['delivery'] != 'delivered':
                    raise ToolRejected('Image edits require a delivered owner artifact')
                self.artifact(original, preflight=True)
            job = uuid.uuid4().hex
            self.store.connection.execute('''INSERT INTO media_jobs
                (id,request_key,kind,payload,digest,state,reserved_usd,created_at,updated_at)
                VALUES (?,?,?,?,?,'queued',?,?,?)''', (job, request_key, kind, payload, digest, reserve, now, now))
            return job

    def execute(self, name, arguments):
        if name == 'profile.propose_avatar' and set(arguments) == {'job_id'}:
            return {'proposal_id': self.propose_avatar(arguments['job_id'])}
        if name == 'media.status' and not arguments:
            return self.status()
        if name == 'media.get' and set(arguments) == {'job_id'} and isinstance(arguments['job_id'], str):
            row = self.job(arguments['job_id'], preflight=True)
            return {key: row[key] for key in ('id', 'kind', 'state', 'delivery', 'mime', 'actual_usd', 'error')}
        if (name in {'voice.generate', 'image.generate', 'image.edit'}
                and set(arguments) in ({'text', 'owner_request_id'}, {'text', 'request_id'})):
            request_id = arguments.get('owner_request_id', arguments.get('request_id'))
            if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,80}', request_id):
                raise ToolRejected('Invalid media request ID')
            authorization = MEDIA_AUTHORIZATION.get()
            if authorization is not None:
                service, kind, authorized_id = authorization
                if service is self and name == kind + '.generate' and request_id == authorized_id:
                    job = self.submit(kind, arguments['text'], request_id, owner_requested=True)
                    return {'job_id': job, 'state': self.job(job)['state']}
                raise ToolRejected('This owner request does not authorize that media action')
            with self.store._lock:
                row = self.store.connection.execute('SELECT * FROM media_jobs WHERE request_key=?', (request_id,)).fetchone()
                if row:
                    if (row['kind'] != ('voice' if name == 'voice.generate' else 'image')
                            or json.loads(row['payload'])['text'] != arguments['text']
                            or (name == 'image.edit' and not json.loads(row['payload']).get('reference_id'))):
                        raise ToolRejected('Media request ID does not match the saved request')
                    return {'job_id': row['id'], 'state': row['state'], 'delivery': row['delivery']}
            kind = 'voice' if name == 'voice.generate' else 'image'
            if name == 'image.edit' or not self.spontaneous_enabled(kind):
                raise ToolRejected('No saved request or standing permission for this media action')
            job = self.submit(kind, arguments['text'], 'agent:' + kind + ':' + request_id)
            return {'job_id': job, 'state': self.job(job)['state'], 'delivery': self.job(job)['delivery']}
        raise ToolRejected('Invalid media tool arguments')

    def job(self, job_id, *, preflight=False):
        error = ToolRejected if preflight else ValueError
        if not isinstance(job_id, str):
            raise error('Invalid media job ID')
        with self.store._lock:
            row = self.store.connection.execute('SELECT * FROM media_jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise error('Unknown media job')
            return dict(row)

    def claim(self):
        if self.store.get_state('restore_recovery_required', '0') == '1':
            return None
        if (not self.policy.enabled or self.store.get_state('paused', '0') == '1'
                or self.store.get_state('sleeping', '0') == '1'):
            return None
        with self.store._lock, self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            expired = self.store.connection.execute("UPDATE media_jobs SET state='cancelled',error='authorization_expired',updated_at=? WHERE state='queued' AND substr(created_at,1,10) != ?", (self.now(), self.now()[:10])).rowcount
            if expired:
                self.store.connection.execute("INSERT INTO telegram_outbox(chat_id,text,created_at,lane) VALUES(?,?,?,'system')",
                                              (self.owner_id, 'Unstarted media requests from an earlier UTC day expired without provider calls. Submit a fresh request if still wanted.', self.now()))
            row = self.store.connection.execute("SELECT * FROM media_jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row:
                # Recheck standing permission before a queued provider call.
                now = self.now()
                kind = row['kind']
                payload = json.loads(row['payload'])
                if ((kind == 'voice' and not self.policy.voice_enabled)
                        or (payload.get('origin') == 'agent' and not self.spontaneous_enabled(kind))):
                    self.store.connection.execute("UPDATE media_jobs SET state='cancelled',error='policy_changed',updated_at=? WHERE id=?", (now, row['id']))
                    self.store.connection.execute("INSERT INTO telegram_outbox(chat_id,text,created_at,lane) VALUES(?,?,?,'system')",
                                                  (self.owner_id, 'Queued media request cancelled because current permissions or budget no longer allow it. No provider call was made. Job: ' + row['id'], now))
                    return None
                self.store.connection.execute("UPDATE media_jobs SET state='running',updated_at=? WHERE id=?", (now, row['id']))
            return dict(row) if row else None

    def recover(self):
        with self.store._lock, self.store.connection:
            self._reconcile_deliveries_locked(startup=True)
            self.store.connection.execute("UPDATE media_jobs SET state='uncertain',error='interrupted' WHERE state='running'")
            sending = self.store.connection.execute("SELECT id FROM media_jobs WHERE state='succeeded' AND delivery='sending'").fetchall()
            for row in sending:
                self._hold_delivery_locked(row['id'], 'interrupted')
            self.store.connection.execute("UPDATE media_avatar_proposals SET state='uncertain' WHERE state='applying'")

    def cancel(self, job_id):
        with self.store._lock, self.store.connection:
            return bool(self.store.connection.execute("UPDATE media_jobs SET state='cancelled',updated_at=? WHERE id=? AND state='queued'", (self.now(), job_id)).rowcount)

    def fail(self, job_id, category='provider_uncertain', *, uncertain=True):
        with self.store._lock, self.store.connection:
            self.store.connection.execute("UPDATE media_jobs SET state=?,error=?,updated_at=? WHERE id=? AND state='running'",
                                          ('uncertain' if uncertain else 'failed', category, self.now(), job_id))

    def complete(self, job_id, data, mime, actual=None):
        row = self.job(job_id)
        if row['state'] != 'running':
            raise ValueError('Media job is not running')
        if not isinstance(data, bytes) or not 32 <= len(data) <= 8 * 1024 * 1024:
            raise ValueError('Invalid media size')
        if row['kind'] == 'voice':
            valid = mime == 'audio/mpeg' and (data.startswith(b'ID3') or data[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'))
            suffix = '.mp3'
        else:
            valid = mime == 'image/png' and validate_png(data)
            suffix = '.png'
        if not valid:
            raise ValueError('Invalid media type')
        if actual is not None and (type(actual) not in (int, float) or not math.isfinite(actual) or actual < 0):
            raise ValueError('Invalid media usage')
        if sum(path.stat().st_size for path in self.artifacts.iterdir() if path.is_file()) + len(data) > 256 * 1024 * 1024:
            raise ValueError('Media artifact storage quota exhausted')
        target = self.artifacts / (row['id'] + suffix)
        temporary = target.with_suffix('.part')
        with temporary.open('xb') as output:
            os.chmod(temporary, 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        directory = os.open(self.artifacts, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        with self.store._lock, self.store.connection:
            self.store.connection.execute("""UPDATE media_jobs SET state='succeeded',mime=?,artifact_sha=?,actual_usd=?,updated_at=?
                WHERE id=? AND state='running'""", (mime, hashlib.sha256(data).hexdigest(), actual, self.now(), job_id))

    def artifact(self, row, *, preflight=False):
        # Workers call this after paid generation/delivery admission. Only the
        # submit/proposal preflight callers may classify explicit checks.
        error = ToolRejected if preflight else ValueError
        if not re.fullmatch(r'[a-f0-9]{32}', row['id']) or row['state'] != 'succeeded':
            raise error('Invalid artifact reference')
        path = self.artifacts / (row['id'] + ('.mp3' if row['kind'] == 'voice' else '.png'))
        if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
            raise error('Invalid artifact')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row['artifact_sha']:
            raise error('Artifact checksum mismatch')
        return data

    def claim_delivery(self):
        if (self.store.get_state('paused', '0') == '1'
                or self.store.get_state('restore_recovery_required', '0') == '1'):
            return None
        with self.store._lock, self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            row = self.store.connection.execute("""SELECT * FROM media_jobs WHERE state='succeeded' AND delivery='pending'
                AND NOT EXISTS (SELECT 1 FROM media_delivery_receipts r WHERE r.job_id=media_jobs.id)
                ORDER BY created_at,id LIMIT 1""").fetchone()
            if row:
                self.store.connection.execute("UPDATE media_jobs SET delivery='sending',delivery_attempt=?,delivery_chat_id=? WHERE id=?",
                                              (uuid.uuid4().hex, self.owner_id, row['id']))
                row = self.store.connection.execute('SELECT * FROM media_jobs WHERE id=?', (row['id'],)).fetchone()
            return dict(row) if row else None

    def save_delivery_receipt(self, row, result):
        message_id, file_identity = validate_sent_media(result, self.owner_id, row['mime'])
        if not row.get('delivery_attempt') or row.get('delivery_chat_id') != self.owner_id:
            raise ValueError('Media delivery admission is missing')
        with self.store._lock, self.store.connection:
            current = self.store.connection.execute('SELECT * FROM media_jobs WHERE id=?', (row['id'],)).fetchone()
            if (current is None or current['state'] != 'succeeded' or current['delivery'] != 'sending'
                    or any(current[key] != row[key] for key in
                           ('delivery_attempt', 'delivery_chat_id', 'kind', 'mime', 'artifact_sha', 'digest'))):
                raise ValueError('Media delivery admission changed')
            self.store.connection.execute('''INSERT INTO media_delivery_receipts
                (attempt_id,job_id,chat_id,kind,mime,artifact_sha,job_digest,message_id,file_identity_sha,confirmed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)''', (row['delivery_attempt'], row['id'], self.owner_id, row['kind'],
                row['mime'], row['artifact_sha'], row['digest'], message_id,
                hashlib.sha256(file_identity.encode()).hexdigest(), self.now()))
        return message_id

    def _reconcile_deliveries_locked(self, *, startup=False):
        if self.store.get_state('restore_recovery_required', '0') == '1':
            return 0
        states = "('sending','uncertain')" if startup else "('uncertain')"
        return self.store.connection.execute('''UPDATE media_jobs SET delivery='delivered',
            telegram_message_id=(SELECT r.message_id FROM media_delivery_receipts r WHERE r.job_id=media_jobs.id),
            error=CASE WHEN error LIKE 'delivery_%' THEN NULL ELSE error END, updated_at=?
            WHERE state='succeeded' AND delivery IN ''' + states + ''' AND EXISTS (
                SELECT 1 FROM media_delivery_receipts r WHERE r.job_id=media_jobs.id
                AND r.attempt_id=media_jobs.delivery_attempt AND r.chat_id=media_jobs.delivery_chat_id
                AND r.chat_id=? AND r.kind=media_jobs.kind AND r.mime=media_jobs.mime
                AND r.artifact_sha=media_jobs.artifact_sha AND r.job_digest=media_jobs.digest
                AND r.message_id>0)''', (self.now(), self.owner_id)).rowcount

    def reconcile_deliveries(self, *, startup=False):
        with self.store._lock, self.store.connection:
            return self._reconcile_deliveries_locked(startup=startup)

    def acknowledge(self, job_id, message_id):
        with self.store._lock, self.store.connection:
            proof = self.store.connection.execute('SELECT message_id FROM media_delivery_receipts WHERE job_id=?', (job_id,)).fetchone()
            if proof is None or type(message_id) is not int or proof['message_id'] != message_id:
                raise ValueError('Media delivery confirmation is missing')
            self._reconcile_deliveries_locked(startup=True)
            row = self.store.connection.execute('SELECT delivery,telegram_message_id FROM media_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row['delivery'] != 'delivered' or row['telegram_message_id'] != message_id:
                raise ValueError('Media delivery confirmation no longer matches')

    def _hold_delivery_locked(self, job_id, category, http_status=None):
        self._reconcile_deliveries_locked(startup=True)
        allowed = TelegramMediaError.CATEGORIES | {'interrupted'}
        category = category if category in allowed else 'unexpected_error'
        status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        diagnostic = 'delivery_' + category + (f'_{status}' if status is not None else '')
        changed = self.store.connection.execute("""UPDATE media_jobs SET delivery='uncertain',error=?,updated_at=?
            WHERE id=? AND state='succeeded' AND delivery='sending'""", (diagnostic, self.now(), job_id)).rowcount
        if changed:
            # One transaction records both the hold and the local notice. An
            # interrupted acknowledgement or repeated recovery cannot resend.
            self.store.connection.execute("INSERT INTO telegram_outbox(chat_id,text,created_at,lane) VALUES(?,?,?,'system')",
                                          (self.owner_id, f'Media delivery could not be confirmed ({diagnostic}). Job {job_id} is held without retry. Check Telegram before requesting a resend.', self.now()))
        return bool(changed)

    def hold_delivery(self, job_id, error=None, *, interrupted=False):
        if interrupted:
            category, status = 'interrupted', None
        elif isinstance(error, TelegramMediaError):
            category, status = error.category, error.http_status
        elif isinstance(error, TimeoutError):
            category, status = 'timeout', None
        elif isinstance(error, OSError):
            category, status = 'network_error', None
        else:
            category, status = 'unexpected_error', None
        with self.store._lock, self.store.connection:
            return self._hold_delivery_locked(job_id, category, status)

    def propose_avatar(self, job_id):
        with self.store._lock, self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            row = self.job(job_id, preflight=True)
            if row['kind'] != 'image' or row['state'] != 'succeeded' or row['delivery'] != 'delivered':
                raise ToolRejected('An image preview must be delivered to the owner first')
            self.artifact(row, preflight=True)
            previous = self.store.connection.execute("SELECT id FROM media_avatar_proposals WHERE job_id=? AND state IN ('proposed','approved','applying')", (job_id,)).fetchone()
            if previous:
                return previous['id']
            proposal = uuid.uuid4().hex
            self.store.connection.execute('INSERT INTO media_avatar_proposals(id,job_id,created_at) VALUES(?,?,?)', (proposal, job_id, self.now()))
        self.store.queue_reply(self.owner_id, f'Avatar proposal {proposal} uses image job {job_id}. To publish it, use /avatar_approve {proposal}.')
        return proposal

    def approve_avatar(self, proposal):
        with self.store._lock, self.store.connection:
            return bool(self.store.connection.execute("UPDATE media_avatar_proposals SET state='approved' WHERE id=? AND state='proposed'", (proposal,)).rowcount)

    def claim_avatar(self):
        if self.store.get_state('restore_recovery_required', '0') == '1':
            return None
        if not self.policy.enabled:
            return None
        with self.store._lock, self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            row = self.store.connection.execute("SELECT * FROM media_avatar_proposals WHERE state='approved' ORDER BY created_at LIMIT 1").fetchone()
            if row:
                self.store.connection.execute("UPDATE media_avatar_proposals SET state='applying' WHERE id=?", (row['id'],))
            return dict(row) if row else None

    def finish_avatar(self, proposal, succeeded):
        with self.store._lock, self.store.connection:
            self.store.connection.execute("UPDATE media_avatar_proposals SET state=? WHERE id=? AND state='applying'", ('applied' if succeeded else 'uncertain', proposal))
        self.store.add_activity('Owner-approved avatar ' + ('applied' if succeeded else 'outcome uncertain; held'))

    def cleanup(self):
        # Retain audit/spend records; remove only old, acknowledged, unreferenced artifacts.
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.policy.retention_days)).isoformat()
        with self.store._lock, self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            rows = self.store.connection.execute("""SELECT * FROM media_jobs WHERE delivery='delivered' AND updated_at<?
                AND id NOT IN (SELECT job_id FROM media_avatar_proposals)
                AND id NOT IN (SELECT json_extract(payload,'$.reference_id') FROM media_jobs WHERE state IN ('queued','running','uncertain') AND json_extract(payload,'$.reference_id') IS NOT NULL)""", (cutoff,)).fetchall()
            for row in rows:
                if not re.fullmatch(r'[a-f0-9]{32}', row['id']):
                    continue
                (self.artifacts / (row['id'] + ('.mp3' if row['kind'] == 'voice' else '.png'))).unlink(missing_ok=True)
                self.store.connection.execute("UPDATE media_jobs SET delivery='expired' WHERE id=?", (row['id'],))

    def status(self):
        now = self.now()
        with self.store._lock:
            result = {'enabled': self.policy.enabled, 'spontaneous_voice': self.spontaneous_enabled('voice'),
                      'spontaneous_image': self.spontaneous_enabled('image'), 'spending_policy': 'agent_managed_api_credits'}
            for kind in ('voice', 'image'):
                reserved = self.store.connection.execute("SELECT coalesce(sum(reserved_usd),0) FROM media_jobs WHERE kind=? AND state IN ('queued','running','uncertain') AND created_at LIKE ?", (kind, now[:7] + '%')).fetchone()[0]
                result[kind] = {'enabled': self.policy.enabled and (kind != 'voice' or self.policy.voice_enabled), 'monthly_usd': self._usage(kind, now[:7]), 'daily_usd': self._usage(kind, now[:10]),
                                'reserved_usd': reserved, 'monthly_cap_usd': None,
                                'daily_cap_usd': None, 'per_job_cap_usd': None}
            result['counts'] = [dict(row) for row in self.store.connection.execute('SELECT kind,state,delivery,count(*) AS count FROM media_jobs GROUP BY kind,state,delivery')]
            result['avatars'] = [dict(row) for row in self.store.connection.execute('SELECT * FROM media_avatar_proposals ORDER BY created_at DESC LIMIT 10')]
            result['jobs'] = [dict(row) for row in self.store.connection.execute('SELECT id,kind,state,delivery,error,created_at,updated_at,(julianday(updated_at)-julianday(created_at))*86400 AS duration_seconds FROM media_jobs ORDER BY created_at DESC LIMIT 10')]
            return result


class MediaWorker:
    def __init__(self, service, provider, telegram):
        self.service, self.provider, self.telegram = service, provider, telegram
        from .latency import LatencyMetrics
        self.metrics = getattr(telegram, "metrics", None)
        if not isinstance(self.metrics, LatencyMetrics):
            self.metrics = LatencyMetrics()
        self.stopping = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is not None:
            raise RuntimeError("Media worker is already started")
        self.service.recover()
        self.thread = threading.Thread(target=self.run, name='andrew-media', daemon=True)
        self.thread.start()

    def request_stop(self):
        """Close admission immediately; an existing attempted call may settle."""
        self.stopping.set()

    def stop(self, timeout=2):
        self.request_stop()
        if self.thread:
            self.thread.join(timeout)
        return self.thread is None or not self.thread.is_alive()

    def _stop_before_attempt(self, record_id, phase):
        if not self.stopping.is_set():
            return False
        statements = {
            'generation': "UPDATE media_jobs SET state='queued' WHERE id=? AND state='running'",
            'delivery': "UPDATE media_jobs SET delivery='pending',delivery_attempt='',delivery_chat_id=NULL WHERE id=? AND delivery='sending'",
            'avatar': "UPDATE media_avatar_proposals SET state='approved' WHERE id=? AND state='applying'",
        }
        with self.service.store._lock, self.service.store.connection:
            self.service.store.connection.execute(statements[phase], (record_id,))
        return True

    def step(self):
        if self.stopping.is_set():
            return
        row = self.service.claim()
        if row:
            if self._stop_before_attempt(row['id'], 'generation'):
                return
            try:
                payload = json.loads(row['payload'])
                if payload.get('reference_id'):
                    payload['reference_png'] = self.service.artifact(self.service.job(payload['reference_id']))
            except Exception:
                # No provider attempt occurred, so this reservation is unspent.
                self.service.fail(row['id'], 'preflight_failed', uncertain=False)
                self.service.store.queue_reply(self.service.owner_id, 'Media preparation failed before generation; no provider call was made. Job ' + row['id'])
                return
            if self._stop_before_attempt(row['id'], 'generation'):
                return
            try:
                with self.metrics.measure('media_generate'):
                    data, mime, cost = self.provider.generate(row['kind'], payload)
                self.service.complete(row['id'], data, mime, cost)
            except Exception:
                # A failed HTTP/validation operation may already have cost money.
                self.service.fail(row['id'])
                self.service.store.queue_reply(self.service.owner_id, 'Media generation outcome uncertain; held without retry. Job ' + row['id'])
        if self.stopping.is_set():
            return
        row = self.service.claim_delivery()
        if row:
            if self._stop_before_attempt(row['id'], 'delivery'):
                return
            try:
                data = self.service.artifact(row)
                if self._stop_before_attempt(row['id'], 'delivery'):
                    return
                result = self.telegram.send_media(self.service.owner_id, data, row['mime'])
                message_id = self.service.save_delivery_receipt(row, result)
                self.service.acknowledge(row['id'], message_id)
            except Exception as error:
                self.service.hold_delivery(row['id'], error)

        if self.stopping.is_set():
            return
        proposal = self.service.claim_avatar()
        if proposal:
            if self._stop_before_attempt(proposal['id'], 'avatar'):
                return
            try:
                from .media_provider import avatar_jpeg
                image = self.service.artifact(self.service.job(proposal['job_id']))
                jpeg = avatar_jpeg(image)
                if self._stop_before_attempt(proposal['id'], 'avatar'):
                    return
                self.telegram.set_profile_photo(jpeg)
                self.service.finish_avatar(proposal['id'], True)
            except Exception:
                self.service.finish_avatar(proposal['id'], False)
            self.service.store.queue_reply(self.service.owner_id, 'Avatar update finished; check /media_status for the recorded outcome.')

    def run(self):
        while not self.stopping.is_set():
            try:
                self.step()
                self.service.cleanup()
            except Exception:
                pass  # never include provider response/prompt/credentials in operational logs
            self.stopping.wait(.5)


MEDIA_TOOLS = [
    {'name': 'profile.propose_avatar', 'description': 'Propose an already delivered image as avatar; the owner must approve with /avatar_approve before publishing.',
     'parameters': {'type': 'object', 'properties': {'job_id': {'type': 'string'}}, 'required': ['job_id'], 'additionalProperties': False}},
    {'name': 'media.status', 'description': 'Read media budgets and job states. Reports standing voice/image permissions and uncapped spending totals.',
     'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'media.get', 'description': 'Read a generated media job by opaque ID. Does not expose paths or source prompts.',
     'parameters': {'type': 'object', 'properties': {'job_id': {'type': 'string'}}, 'required': ['job_id'], 'additionalProperties': False}},
    *[{'name': kind + '.generate', 'description': 'Generate and send a voice note or image to the owner when standing permission is enabled, including during autonomous work. Choose a stable request_id for each distinct job; reuse it to inspect the same job. For /voice_reply or /image_reply use the provided owner_request_id. Check media.status and check_budget; credits are finite. Never retry an uncertain job with a new ID.',
       'parameters': {'type': 'object', 'properties': {'text': {'type': 'string', 'maxLength': 2000}, 'owner_request_id': {'type': 'string'}, 'request_id': {'type': 'string', 'maxLength': 80}},
                      'required': ['text'], 'additionalProperties': False}}
      for kind in ('voice', 'image')]
    + [{'name': 'image.edit', 'description': 'Read the image edit job authorized by an exact owner /image_edit request. Cannot create new authorization.',
        'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}, 'owner_request_id': {'type': 'string'}},
                       'required': ['text', 'owner_request_id'], 'additionalProperties': False}}]
]
for _tool in MEDIA_TOOLS:
    _tool['label'] = _tool['name']
MEDIA_TOOL_NAMES = frozenset(tool['name'] for tool in MEDIA_TOOLS)
