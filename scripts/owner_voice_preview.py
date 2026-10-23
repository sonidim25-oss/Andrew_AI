#!/usr/bin/env python3
"""One owner-approved voice preview, isolated from Andrew's live media queue."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3

VOICE = 'pvY1pikBdoI4SB62vEVo'
MODEL = 'elevenlabs/eleven-v3'
TEXT = 'Привет! Это новый голос Andrew. Hello! This is Andrew’s new voice.'
DIRECTORY = Path('/var/lib/andrew/control/owner-voice-preview-20261023')


def preview(directory, provider):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {'voice': VOICE, 'model': MODEL, 'text': TEXT}
    request_sha = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    artifact = directory / 'sample.mp3'
    db = sqlite3.connect(directory / 'preview.sqlite3')
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS preview(id INTEGER PRIMARY KEY CHECK(id=1), '
                   'request_sha TEXT NOT NULL,state TEXT NOT NULL,audio_sha TEXT)')
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT OR IGNORE INTO preview VALUES(1,?,'queued',NULL)", (request_sha,))
            saved_sha, state, audio_sha = db.execute('SELECT request_sha,state,audio_sha FROM preview WHERE id=1').fetchone()
            if saved_sha != request_sha:
                raise ValueError('Saved voice preview request differs')
            if state in {'running', 'uncertain'}:
                raise ValueError('Voice preview outcome uncertain; regeneration refused')
            if state == 'queued':
                db.execute("UPDATE preview SET state='running' WHERE id=1")
            elif state != 'succeeded':
                raise ValueError('Invalid voice preview state')
        if state == 'queued':
            try:
                data, mime, _cost = provider.generate('voice', payload)
                if (not isinstance(data, bytes) or not 32 <= len(data) <= 8 * 1024 * 1024
                        or mime != 'audio/mpeg'
                        or not (data.startswith(b'ID3') or data[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'))):
                    raise ValueError('Invalid voice preview audio')
                with artifact.open('xb') as stream:
                    os.chmod(artifact, 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                audio_sha = hashlib.sha256(data).hexdigest()
                with db:
                    db.execute("UPDATE preview SET state='succeeded',audio_sha=? WHERE id=1", (audio_sha,))
            except Exception:
                with db:
                    db.execute("UPDATE preview SET state='uncertain' WHERE id=1")
                raise ValueError('Voice preview outcome uncertain; regeneration refused') from None
        if (artifact.is_symlink() or not artifact.is_file() or artifact.stat().st_size > 8 * 1024 * 1024
                or hashlib.sha256(artifact.read_bytes()).hexdigest() != audio_sha):
            raise ValueError('Saved voice preview audio changed')
        return {'voice': VOICE, 'model': MODEL, 'state': 'succeeded',
                'artifact': str(artifact), 'sha256': audio_sha}
    finally:
        db.close()


def main():
    from andrewbot.media_provider import OpenRouterMedia
    from andrewbot.vault import Vault
    vault = Vault('/var/lib/andrew/vault')
    try:
        key = vault.get_secret('openrouter_api')
    finally:
        vault.close()
    print(json.dumps(preview(DIRECTORY, OpenRouterMedia(key))), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'state': 'stopped', 'automatic_retry': False}), flush=True)
        raise SystemExit(2)
