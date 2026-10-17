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
from .telegram import TelegramMediaError


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
    voice: str = 'george'
    tts_model: str = 'elevenlabs/eleven-v3'
    image_model: str = 'openai/gpt-image-1-mini'
    monthly_usd: float = 3.0  # per capability, separate from reasoning
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
        return MediaPolicy(enabled=enabled, voice_enabled=environment.get('ANDREW_MEDIA_VOICE_ENABLED', '1') == '1', voice=environment.get('ANDREW_TTS_VOICE', 'george'),
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
            ''')

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def _usage(self, kind, period):
        row = self.store.connection.execute('''SELECT coalesce(sum(CASE
            WHEN state='succeeded' THEN coalesce(actual_usd,reserved_usd)
            WHEN state IN ('queued','running','uncertain') THEN reserved_usd ELSE 0 END),0)
            FROM media_jobs WHERE kind=? AND created_at LIKE ?''', (kind, period + '%')).fetchone()
        return float(row[0])

    def submit(self, kind, text, request_key, *, owner_requested=False, voice_id=None, reference_id=None):
        if self.store.get_state('restore_recovery_required', '0') == '1':
            raise ValueError('Restore requires billing and action reconciliation')
        if not self.policy.enabled or (kind == 'voice' and not self.policy.voice_enabled):
            raise ValueError('Media generation is disabled')
        if kind not in {'voice', 'image'} or not owner_requested:
            raise ValueError('An explicit owner media request is required')
        maximum = 1000 if kind == 'voice' else 2000
        if not isinstance(text, str) or not text.strip() or len(text) > maximum:
            raise ValueError('Media input is empty or too long')
        if not isinstance(request_key, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}', request_key):
            raise ValueError('Invalid media request ID')
        if voice_id is not None and voice_id != self.policy.voice:
            raise ValueError('Only the configured voice is approved')
        if reference_id is not None:
            if kind != 'image' or not isinstance(reference_id, str) or not re.fullmatch(r'[a-f0-9]{32}', reference_id):
                raise ValueError('Invalid image edit artifact')
        # The controller never accepts arbitrary URLs, paths, recipients, or provider settings.
        payload = json.dumps({'text': text, 'voice': self.policy.voice,
                              'model': self.policy.tts_model if kind == 'voice' else self.policy.image_model,
                              'reference_id': reference_id},
                             ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256((kind + payload).encode()).hexdigest()
        reserve = len(text) * self.policy.voice_character_usd if kind == 'voice' else self.policy.image_job_usd
        if reserve > (self.policy.voice_job_usd if kind == 'voice' else self.policy.image_job_usd):
            raise ValueError('Media request exceeds the per-job budget')
        now = self.now()
        with self.store._lock, self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            old = self.store.connection.execute('SELECT id,digest FROM media_jobs WHERE request_key=?', (request_key,)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ValueError('Media request ID was already used with different arguments')
                return old['id']
            if reference_id is not None:
                # Cleanup must not remove the source between validation and
                # insertion of the durable reference, including other stores.
                original = self.job(reference_id)
                if original['kind'] != 'image' or original['delivery'] != 'delivered':
                    raise ValueError('Image edits require a delivered owner artifact')
                self.artifact(original)
            count = self.store.connection.execute("SELECT count(*) FROM media_jobs WHERE kind=? AND created_at LIKE ? AND state!='cancelled'", (kind, now[:10] + '%')).fetchone()[0]
            if count >= (20 if kind == 'voice' else 2):
                raise ValueError('Media daily job quota exhausted')
            if (self._usage(kind, now[:7]) + reserve > self.policy.monthly_usd + 1e-9
                    or self._usage(kind, now[:10]) + reserve > self.policy.daily_usd + 1e-9):
                raise ValueError('Media budget exhausted')
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
            row = self.job(arguments['job_id'])
            return {key: row[key] for key in ('id', 'kind', 'state', 'delivery', 'mime', 'actual_usd', 'error')}
        if name in {'voice.generate', 'image.generate', 'image.edit'} and set(arguments) == {'text', 'owner_request_id'}:
            authorization = MEDIA_AUTHORIZATION.get()
            if authorization is not None:
                service, kind, request_id = authorization
                if service is self and name == kind + '.generate' and arguments['owner_request_id'] == request_id:
                    job = self.submit(kind, arguments['text'], request_id, owner_requested=True)
                    return {'job_id': job, 'state': self.job(job)['state']}
                raise ValueError('This owner request does not authorize that media action')
            # The owner command creates the immutable, exact-content authorization.
            # Calling this tool cannot create one, even during an autonomous turn.
            with self.store._lock:
                row = self.store.connection.execute('SELECT * FROM media_jobs WHERE request_key=?', (arguments['owner_request_id'],)).fetchone()
                if (not row or row['kind'] != ('voice' if name == 'voice.generate' else 'image')
                        or json.loads(row['payload'])['text'] != arguments['text']
                        or (name == 'image.edit' and not json.loads(row['payload']).get('reference_id'))):
                    raise ValueError('An exact saved owner media request is required; ask the owner to use /voice or /image')
                return {'job_id': row['id'], 'state': row['state'], 'delivery': row['delivery']}
        raise ValueError('Invalid media tool arguments')

    def job(self, job_id):
        with self.store._lock:
            row = self.store.connection.execute('SELECT * FROM media_jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise ValueError('Unknown media job')
            return dict(row)

    def claim(self):
        if self.store.get_state('restore_recovery_required', '0') == '1':
            return None
        if not self.policy.enabled:
            return None
        with self.store._lock, self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            expired = self.store.connection.execute("UPDATE media_jobs SET state='cancelled',error='authorization_expired',updated_at=? WHERE state='queued' AND substr(created_at,1,10) != ?", (self.now(), self.now()[:10])).rowcount
            if expired:
                self.store.connection.execute("INSERT INTO telegram_outbox(chat_id,text,created_at,lane) VALUES(?,?,?,'system')",
                                              (self.owner_id, 'Unstarted media requests from an earlier UTC day expired without provider calls. Submit a fresh request if still wanted.', self.now()))
            row = self.store.connection.execute("SELECT * FROM media_jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row:
                # Reservations and policy checks share the write transaction with
                # admission. Restarted queues cannot spend under an older policy.
                now = self.now()
                kind = row['kind']
                payload = json.loads(row['payload'])
                estimate = (len(payload['text']) * self.policy.voice_character_usd
                            if kind == 'voice' else self.policy.image_job_usd)
                cap = self.policy.voice_job_usd if kind == 'voice' else self.policy.image_job_usd
                if ((kind == 'voice' and not self.policy.voice_enabled)
                        or row['reserved_usd'] > cap + 1e-9
                        or estimate > row['reserved_usd'] + 1e-9
                        or self._usage(kind, now[:7]) > self.policy.monthly_usd + 1e-9
                        or self._usage(kind, now[:10]) > self.policy.daily_usd + 1e-9):
                    self.store.connection.execute("UPDATE media_jobs SET state='cancelled',error='policy_changed',updated_at=? WHERE id=?", (now, row['id']))
                    self.store.connection.execute("INSERT INTO telegram_outbox(chat_id,text,created_at,lane) VALUES(?,?,?,'system')",
                                                  (self.owner_id, 'Queued media request cancelled because current permissions or budget no longer allow it. No provider call was made. Job: ' + row['id'], now))
                    return None
                self.store.connection.execute("UPDATE media_jobs SET state='running',updated_at=? WHERE id=?", (now, row['id']))
            return dict(row) if row else None

    def recover(self):
        with self.store._lock, self.store.connection:
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

    def artifact(self, row):
        if not re.fullmatch(r'[a-f0-9]{32}', row['id']) or row['state'] != 'succeeded':
            raise ValueError('Invalid artifact reference')
        path = self.artifacts / (row['id'] + ('.mp3' if row['kind'] == 'voice' else '.png'))
        if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('Invalid artifact')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row['artifact_sha']:
            raise ValueError('Artifact checksum mismatch')
        return data

    def claim_delivery(self):
        if self.store.get_state('restore_recovery_required', '0') == '1':
            return None
        with self.store._lock, self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            row = self.store.connection.execute("SELECT * FROM media_jobs WHERE state='succeeded' AND delivery='pending' ORDER BY created_at,id LIMIT 1").fetchone()
            if row:
                self.store.connection.execute("UPDATE media_jobs SET delivery='sending' WHERE id=?", (row['id'],))
            return dict(row) if row else None

    def acknowledge(self, job_id, message_id):
        with self.store._lock, self.store.connection:
            self.store.connection.execute("UPDATE media_jobs SET delivery='delivered',telegram_message_id=?,updated_at=? WHERE id=? AND delivery='sending'", (message_id, self.now(), job_id))

    def _hold_delivery_locked(self, job_id, category, http_status=None):
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
            row = self.job(job_id)
            if row['kind'] != 'image' or row['state'] != 'succeeded' or row['delivery'] != 'delivered':
                raise ValueError('An image preview must be delivered to the owner first')
            self.artifact(row)
            previous = self.store.connection.execute("SELECT id FROM media_avatar_proposals WHERE job_id=? AND state IN ('proposed','approved','applying','applied')", (job_id,)).fetchone()
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
            result = {'enabled': self.policy.enabled, 'spontaneous_voice': False}
            for kind in ('voice', 'image'):
                reserved = self.store.connection.execute("SELECT coalesce(sum(reserved_usd),0) FROM media_jobs WHERE kind=? AND state IN ('queued','running','uncertain') AND created_at LIKE ?", (kind, now[:7] + '%')).fetchone()[0]
                result[kind] = {'enabled': self.policy.enabled and (kind != 'voice' or self.policy.voice_enabled), 'monthly_usd': self._usage(kind, now[:7]), 'daily_usd': self._usage(kind, now[:10]),
                                'reserved_usd': reserved, 'monthly_cap_usd': self.policy.monthly_usd,
                                'daily_cap_usd': self.policy.daily_usd}
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
            'delivery': "UPDATE media_jobs SET delivery='pending' WHERE id=? AND delivery='sending'",
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
                self.service.acknowledge(row['id'], result['message_id'])
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
    {'name': 'media.status', 'description': 'Read media budgets and job states. Media authorization comes only from owner /voice and /image commands.',
     'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'media.get', 'description': 'Read a generated media job by opaque ID. Does not expose paths or source prompts.',
     'parameters': {'type': 'object', 'properties': {'job_id': {'type': 'string'}}, 'required': ['job_id'], 'additionalProperties': False}},
    *[{'name': kind + '.generate', 'description': 'Generate one response during an explicitly authorized /voice_reply or /image_reply owner turn using its owner_request_id. Otherwise return the exact saved /voice or /image job. Delivery runs in the background; arbitrary recipients and spontaneous spending are denied.',
       'parameters': {'type': 'object', 'properties': {'text': {'type': 'string', 'maxLength': 2000}, 'owner_request_id': {'type': 'string'}},
                      'required': ['text', 'owner_request_id'], 'additionalProperties': False}}
      for kind in ('voice', 'image')]
    + [{'name': 'image.edit', 'description': 'Read the image edit job authorized by an exact owner /image_edit request. Cannot create new authorization.',
        'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}, 'owner_request_id': {'type': 'string'}},
                       'required': ['text', 'owner_request_id'], 'additionalProperties': False}}]
]
for _tool in MEDIA_TOOLS:
    _tool['label'] = _tool['name']
MEDIA_TOOL_NAMES = frozenset(tool['name'] for tool in MEDIA_TOOLS)
