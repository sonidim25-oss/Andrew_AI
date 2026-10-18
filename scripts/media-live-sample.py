#!/usr/bin/env python3
"""One explicitly approved voice/image probe; durable claims prevent replay."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'prototype'))
from andrewbot.media import MediaPolicy, MediaService
from andrewbot.media_provider import OpenRouterMedia
from andrewbot.store import Store
from andrewbot.telegram import TelegramAPI
from andrewbot.vault import Vault


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--approved-sample', action='store_true', help='Run only after explicit owner approval of the two samples')
    args = parser.parse_args()
    root = Path(os.environ['ANDREW_DATA_DIR'])
    owner_id = int(os.environ['ANDREW_OWNER_ID'])
    # Public discovery is free and never uses the account key.
    for endpoint, model, voice in (
            ('models?output_modalities=speech', 'elevenlabs/eleven-v3', 'george'),
            ('images/models', 'openai/gpt-image-1-mini', None)):
        with urlopen('https://openrouter.ai/api/v1/' + endpoint, timeout=20) as response:
            data = json.load(response)
        entry = next(item for item in data['data'] if item['id'] == model)
        if voice and voice not in entry.get('supported_voices', []):
            raise ValueError('Approved voice is not advertised')
    vault = Vault(os.environ['ANDREW_VAULT_DIR'])
    try:
        key, token = vault.get_secret('openrouter_api'), vault.get_secret('telegram_bot')
    finally:
        vault.close()
    telegram = TelegramAPI(token)
    telegram.get_me()
    if not args.approved_sample:
        print(json.dumps({'preflight': 'passed', 'paid_requests': 0}))
        return 0
    store = Store(root / 'controller.sqlite3')
    try:
        media = MediaService(store, root / 'media-artifacts', owner_id, MediaPolicy(enabled=True))
        provider = OpenRouterMedia(key)
        samples = [
            ('voice', 'Привет! Это тест моего голоса Eleven v3. Hello! This is Andrew’s voice sample.'),
            ('image', 'A small friendly blue robot reading a book beside a leafy plant, warm desk light, simple clean illustration, square composition, no text.'),
        ]
        # Fixed authorization IDs: repeating this script cannot authorize another charge.
        jobs = [(kind, media.submit(kind, text, 'approved-sample-20261017-1-' + kind, owner_requested=True))
                for kind, text in samples]
        if sum(media.job(job)['reserved_usd'] for _, job in jobs) > .15:
            raise ValueError('Approved sample estimate exceeded')
        for kind, job in jobs:
            row = media.job(job)
            if row['state'] == 'running':
                media.fail(job, 'interrupted_approved_sample')
            if row['delivery'] == 'sending':
                media.hold_delivery(job, interrupted=True)
            row = media.job(job)
            if row['state'] == 'queued':
                with store._lock, store.connection:
                    claimed = store.connection.execute("UPDATE media_jobs SET state='running',updated_at=? WHERE id=? AND state='queued'", (media.now(), job)).rowcount
                if claimed:
                    print(json.dumps({'kind': kind, 'job_id': job, 'generation': 'started'}), flush=True)
                    try:
                        payload = json.loads(row['payload'])
                        data, mime, cost = provider.generate(kind, payload)
                        media.complete(job, data, mime, cost)
                    except Exception:
                        media.fail(job, 'approved_sample_uncertain')
            row = media.job(job)
            if row['state'] == 'succeeded' and row['delivery'] == 'pending':
                with store._lock, store.connection:
                    store.connection.execute('BEGIN IMMEDIATE')
                    claimed = store.connection.execute("""UPDATE media_jobs
                        SET delivery='sending',delivery_attempt=?,delivery_chat_id=?
                        WHERE id=? AND delivery='pending' AND NOT EXISTS
                        (SELECT 1 FROM media_delivery_receipts WHERE job_id=media_jobs.id)""",
                        (uuid.uuid4().hex, owner_id, job)).rowcount
                    row = media.job(job)
                if claimed:
                    try:
                        caption = 'Approved sample: ' + ('Eleven v3 / George' if kind == 'voice' else 'GPT Image 1 Mini / low quality')
                        result = telegram.send_media(owner_id, media.artifact(row), row['mime'], caption=caption)
                        message_id = media.save_delivery_receipt(row, result)
                        media.acknowledge(job, message_id)
                    except Exception as error:
                        media.hold_delivery(job, error)
            row = media.job(job)
            print(json.dumps({key: row[key] for key in ('id', 'kind', 'state', 'delivery', 'reserved_usd', 'actual_usd', 'telegram_message_id', 'error')}), flush=True)
        report = {'owner_approved': True, 'scope': 'one bilingual Eleven v3 voice sample and one low-quality GPT Image 1 Mini image',
                  'jobs': [{key: media.job(job)[key] for key in ('id', 'kind', 'state', 'delivery', 'reserved_usd', 'actual_usd', 'telegram_message_id', 'error')} for _, job in jobs]}
        target = root / 'media-sample-20261017.json'
        temporary = target.with_suffix('.part')
        with temporary.open('w') as output:
            os.chmod(temporary, 0o600)
            json.dump(report, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        return 0 if all(row['delivery'] == 'delivered' for row in report['jobs']) else 2
    finally:
        store.close()


if __name__ == '__main__':
    if os.geteuid() == 0:
        import pwd
        account = pwd.getpwnam('andrew-control')
        os.initgroups(account.pw_name, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'result': 'stopped', 'error_type': type(error).__name__, 'automatic_retry': False}), flush=True)
        raise SystemExit(2)
